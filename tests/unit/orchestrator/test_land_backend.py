"""LandBackend integration-ish tests against throwaway git repos.

Each test builds a real git repo + bare origin + parcel worktree under
``tmp_path`` and drives :class:`LandBackend` end-to-end against them. Real
``git`` and real subprocesses — no mocks — because the whole point of W11
is proving the subprocess chain does the right thing under failure
conditions that mocks cannot reproduce (rebase conflicts, non-FF races,
subprocess timeouts).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_fleet.orchestrator.backend import Job
from claude_fleet.orchestrator.land_backend import LandBackend
from claude_fleet.orchestrator.land_events import LandResult

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Fixtures: throwaway git rig
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``git -C cwd <args>`` synchronously and capture output."""
    env = dict(os.environ)
    # Deterministic identity for fabricated commits (no reliance on host
    # git config, which may be unset in CI).
    env.setdefault("GIT_AUTHOR_NAME", "claude-fleet Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@claude-fleet.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "claude-fleet Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@claude-fleet.invalid")
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class Rig:
    """Three co-operating repos: origin (bare), main clone, worktree."""

    def __init__(
        self,
        origin: Path,
        main_repo: Path,
        worktree: Path,
        job_id: str,
    ) -> None:
        self.origin = origin
        self.main_repo = main_repo
        self.worktree = worktree
        self.job_id = job_id

    def advance_origin_main(self, filename: str = "race.txt") -> None:
        """Add a commit to origin/main *out of band* (simulates a concurrent
        push between our rebase and FF steps). Done by cloning origin in a
        scratch dir, committing, and pushing back."""
        scratch = self.origin.parent / "scratch-advance"
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
        _git(self.origin.parent, "clone", str(self.origin), str(scratch))
        _write(scratch / filename, "concurrent change\n")
        _git(scratch, "add", filename)
        _git(scratch, "commit", "-m", "concurrent: advance main")
        _git(scratch, "push", "origin", "main")
        # Refresh the main clone's view of origin/main so the non-FF race is
        # visible when LandBackend does its fetch.
        _git(self.main_repo, "fetch", "origin", "main")

    def add_conflicting_main_commit(self) -> None:
        """Commit directly on the main-repo's local ``main`` and push to
        origin, using content that conflicts with the parcel branch."""
        _git(self.main_repo, "checkout", "main")
        _write(self.main_repo / "shared.txt", "main side\n")
        _git(self.main_repo, "add", "shared.txt")
        _git(self.main_repo, "commit", "-m", "main: touch shared")
        _git(self.main_repo, "push", "origin", "main")


@pytest.fixture()
def rig(tmp_path: Path) -> Iterator[Rig]:
    """Build origin (bare) + main clone + parcel worktree with one head commit.

    Layout:
        tmp_path/
            origin.git/                 (bare)
            main/                       (working clone; parcel lives here
                                         as a sibling worktree)
            worktrees/<job_id>/         (parcel worktree checked out on
                                         parcel/<job_id>)
    """

    origin = tmp_path / "origin.git"
    main_repo = tmp_path / "main"
    worktrees_root = tmp_path / "worktrees"
    job_id = "T1"

    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )

    subprocess.run(
        ["git", "init", "-b", "main", str(main_repo)],
        check=True, capture_output=True,
    )
    _git(main_repo, "remote", "add", "origin", str(origin))
    _write(main_repo / "README.md", "seed\n")
    _git(main_repo, "add", "README.md")
    _git(main_repo, "commit", "-m", "initial")
    _git(main_repo, "push", "-u", "origin", "main")

    # Create parcel branch off main, then check it out as a worktree.
    _git(main_repo, "branch", f"parcel/{job_id}")
    worktree = worktrees_root / job_id
    _git(main_repo, "worktree", "add", str(worktree), f"parcel/{job_id}")

    # One commit on the parcel branch so it's ahead of main.
    _write(worktree / "feature.txt", "parcel change\n")
    _git(worktree, "add", "feature.txt")
    _git(worktree, "commit", "-m", "feat: parcel head")

    rig = Rig(origin, main_repo, worktree, job_id)
    try:
        yield rig
    finally:
        # tmp_path is auto-cleaned, but make sure the worktree isn't
        # holding a lock that blocks rmtree on Windows.
        with contextlib.suppress(OSError, subprocess.CalledProcessError):
            _git(main_repo, "worktree", "remove", "--force", str(worktree))


def _make_job(job_id: str, *, attempts: int = 1) -> Job:
    return Job(
        id=job_id,
        prompt_path=f"/tmp/{job_id}.md",
        status="landing",
        audit_iter=1,
        audit_verdict="PASS",
        land_attempts=attempts,
    )


async def _harvest_until_done(
    backend: LandBackend, handle: object, *, timeout: float = 30.0
) -> LandResult:
    """Poll :meth:`LandBackend.harvest` until it returns a terminal result."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = await backend.harvest(handle)  # type: ignore[arg-type]
        if result is not None:
            return result
        if loop.time() > deadline:
            raise AssertionError("LandBackend did not terminate in time")
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_ok_path_fast_forwards_main(rig: Rig) -> None:
    events: list[tuple[object, ...]] = []

    def sink(
        job_id: str,
        attempt: int,
        phase: str,
        outcome: str,
        *,
        detail: str | None = None,
        commit_sha: str | None = None,
    ) -> None:
        events.append((job_id, attempt, phase, outcome, detail, commit_sha))

    backend = LandBackend(
        rig.main_repo,
        worktrees_root=rig.worktree.parent,
        test_cmd="git --version",  # fast, platform-agnostic, always green
        timeout_s=60,
        event_sink=sink,
    )

    handle = await backend.spawn_land(_make_job(rig.job_id))
    result = await _harvest_until_done(backend, handle)

    assert result.outcome == "ok"
    assert result.commit is not None and len(result.commit) >= 40
    # main is now at the parcel head
    head = _git(rig.main_repo, "rev-parse", "HEAD").stdout.strip()
    assert head == result.commit

    # phase events in order
    phases = [row[2] for row in events]
    assert phases == ["start", "rebase", "tests", "ff", "done"]
    # terminal event carries the sha
    assert events[-1][3] == "ok"
    assert events[-1][5] == result.commit


# ---------------------------------------------------------------------------
# Rebase conflict
# ---------------------------------------------------------------------------


async def test_rebase_conflict_aborts_and_returns_conflict(rig: Rig) -> None:
    # Introduce a conflicting commit on both sides of the same file.
    _write(rig.worktree / "shared.txt", "parcel side\n")
    _git(rig.worktree, "add", "shared.txt")
    _git(rig.worktree, "commit", "-m", "parcel: touch shared")
    rig.add_conflicting_main_commit()

    backend = LandBackend(
        rig.main_repo,
        worktrees_root=rig.worktree.parent,
        test_cmd="git --version",
        timeout_s=60,
    )
    handle = await backend.spawn_land(_make_job(rig.job_id))
    result = await _harvest_until_done(backend, handle)

    assert result.outcome == "conflict"
    # Repo left clean: no in-progress rebase.
    status = _git(rig.worktree, "status", "--porcelain", check=False)
    assert "rebase in progress" not in (status.stdout + status.stderr).lower()
    assert not (rig.worktree / ".git" / "rebase-merge").exists()
    assert not (rig.worktree / ".git" / "rebase-apply").exists()
    # main unchanged (no FF happened)
    head = _git(rig.main_repo, "rev-parse", "main").stdout.strip()
    parcel_head = _git(rig.worktree, "rev-parse", "HEAD").stdout.strip()
    assert head != parcel_head


# ---------------------------------------------------------------------------
# Tests fail post-rebase
# ---------------------------------------------------------------------------


async def test_tests_fail_post_rebase_returns_failed(rig: Rig) -> None:
    # "exit 1" under Windows cmd and POSIX sh both set a non-zero rc.
    test_cmd = "cmd /c exit 1" if IS_WINDOWS else "false"
    backend = LandBackend(
        rig.main_repo,
        worktrees_root=rig.worktree.parent,
        test_cmd=test_cmd,
        timeout_s=60,
    )
    handle = await backend.spawn_land(_make_job(rig.job_id))
    result = await _harvest_until_done(backend, handle)

    assert result.outcome == "failed"
    # main did NOT advance (FF step was never reached)
    main_head = _git(rig.main_repo, "rev-parse", "main").stdout.strip()
    seed = _git(rig.main_repo, "rev-parse", "origin/main").stdout.strip()
    assert main_head == seed


# ---------------------------------------------------------------------------
# Non-FF race: main moves between our rebase and our FF step.
# ---------------------------------------------------------------------------


async def test_non_ff_race_returns_non_ff(rig: Rig, tmp_path: Path) -> None:
    """Advance *local* main to a new commit between rebase and FF, triggering
    the non-FF code path."""

    async def slow_test_cmd() -> str:
        """Return the path to a script that sleeps then advances local main."""
        script = tmp_path / ("slow_test.bat" if IS_WINDOWS else "slow_test.sh")
        marker = tmp_path / "advance-marker"
        if IS_WINDOWS:
            script.write_text(
                "@echo off\r\n"
                f"echo marker > {marker}\r\n"
                "ping -n 2 127.0.0.1 > nul\r\n"
                "exit 0\r\n",
                encoding="ascii",
            )
        else:
            script.write_text(
                "#!/bin/sh\n"
                f"touch {marker}\n"
                "sleep 1\n"
                "exit 0\n",
                encoding="ascii",
            )
            os.chmod(script, 0o755)
        return str(script)

    script = await slow_test_cmd()

    # Advance local main *first* — simpler and deterministic than a real
    # race, and triggers exactly the same non-FF code path.
    _write(rig.main_repo / "hotfix.txt", "hotfix landed\n")
    _git(rig.main_repo, "checkout", "main")
    _git(rig.main_repo, "add", "hotfix.txt")
    _git(rig.main_repo, "commit", "-m", "hotfix on main")

    backend = LandBackend(
        rig.main_repo,
        worktrees_root=rig.worktree.parent,
        test_cmd=script,
        timeout_s=60,
    )
    handle = await backend.spawn_land(_make_job(rig.job_id))
    result = await _harvest_until_done(backend, handle)

    assert result.outcome == "non-ff"
    # local main is still on the hotfix we added; parcel did NOT overwrite.
    hotfix_present = (rig.main_repo / "hotfix.txt").exists()
    assert hotfix_present


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


async def test_timeout_kills_subprocess_and_returns_timeout(
    rig: Rig, tmp_path: Path
) -> None:
    # A test command that sleeps longer than the backend's timeout.
    if IS_WINDOWS:
        script = tmp_path / "sleep30.bat"
        script.write_text(
            "@echo off\r\nping -n 60 127.0.0.1 > nul\r\nexit 0\r\n",
            encoding="ascii",
        )
        test_cmd = str(script)
    else:
        test_cmd = "sleep 30"

    backend = LandBackend(
        rig.main_repo,
        worktrees_root=rig.worktree.parent,
        test_cmd=test_cmd,
        timeout_s=2,  # 2 seconds
    )
    handle = await backend.spawn_land(_make_job(rig.job_id))
    result = await _harvest_until_done(backend, handle, timeout=20.0)

    assert result.outcome == "timeout"
    # Rebase left clean: backend's timeout handler runs --abort.
    assert not (rig.worktree / ".git" / "rebase-merge").exists()
    assert not (rig.worktree / ".git" / "rebase-apply").exists()


# ---------------------------------------------------------------------------
# Contract checks
# ---------------------------------------------------------------------------


async def test_spawn_land_refuses_zero_attempts(rig: Rig) -> None:
    backend = LandBackend(rig.main_repo, worktrees_root=rig.worktree.parent)
    with pytest.raises(ValueError, match="land_attempts>=1"):
        await backend.spawn_land(_make_job(rig.job_id, attempts=0))


async def test_terminate_before_done_cancels_chain(rig: Rig, tmp_path: Path) -> None:
    # Slow test command so we can terminate mid-flight.
    if IS_WINDOWS:
        script = tmp_path / "long_sleep.bat"
        script.write_text(
            "@echo off\r\nping -n 30 127.0.0.1 > nul\r\nexit 0\r\n",
            encoding="ascii",
        )
        test_cmd = str(script)
    else:
        test_cmd = "sleep 15"

    backend = LandBackend(
        rig.main_repo,
        worktrees_root=rig.worktree.parent,
        test_cmd=test_cmd,
        timeout_s=60,
    )
    handle = await backend.spawn_land(_make_job(rig.job_id))
    # Let the chain get into the rebase step.
    await asyncio.sleep(0.5)
    await backend.terminate(handle)
    # The task is now done; harvest returns a terminal LandResult.
    result = await backend.harvest(handle)
    assert result is not None
    assert result.outcome == "timeout"
