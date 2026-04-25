"""G2 worktree auto-retire tests.

Covers:
- Daemon wiring: retire called on landed transition when auto_retire=True
- Daemon wiring: retire NOT called when auto_retire=False
- retire() on uncommitted changes → raises WorktreeRetireError
- Windows file-lock simulation → succeeds after retries
- CLI ``worktree retire <id>`` happy path
- CLI ``worktree retire <id>`` on non-landed job → exit 1

These are separate from ``test_worktree.py`` (G1) so G1 tests remain
scoped to create/exists/retire-non-landed. G2 focuses on the new
dirty-state gate, retry logic, daemon wiring, and CLI.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from claude_fleet.orchestrator.backend import Job
from claude_fleet.worktree import GitWorktreeLifecycle, WorktreeRetireError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_job(job_id: str, status: str = "landed") -> Job:
    return Job(
        id=job_id,
        prompt_path=f"parcels/{job_id}.md",
        status=status,  # type: ignore[arg-type]
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="session")
def source_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Minimal git repo for G2 integration tests."""
    repo = tmp_path_factory.mktemp("g2_source_repo")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture()
def worktrees_dir(tmp_path: Path) -> Path:
    d = tmp_path / "worktrees"
    d.mkdir()
    return d


@pytest.fixture()
def lifecycle(source_repo: Path, worktrees_dir: Path) -> GitWorktreeLifecycle:
    return GitWorktreeLifecycle(
        base_dir=worktrees_dir,
        branch_prefix="parcel/",
        base_branch="main",
        repo_root=source_repo,
    )


# ---------------------------------------------------------------------------
# retire() on uncommitted changes
# ---------------------------------------------------------------------------


async def test_retire_with_uncommitted_changes_raises(
    lifecycle: GitWorktreeLifecycle,
    source_repo: Path,
    worktrees_dir: Path,
) -> None:
    """retire() refuses when the worktree has uncommitted changes."""
    job_create = _make_job("g2-dirty-001")
    worktree = await lifecycle.create(job_create)
    assert worktree.is_dir()

    # Write an untracked file so git status --porcelain is non-empty.
    (worktree / "dirty.txt").write_text("not committed\n")

    job_landed = _make_job("g2-dirty-001", status="landed")
    with pytest.raises(WorktreeRetireError, match="uncommitted changes"):
        await lifecycle.retire(job_landed, worktree)

    # Worktree must still exist — nothing was removed.
    assert worktree.is_dir()


async def test_retire_with_staged_changes_raises(
    lifecycle: GitWorktreeLifecycle,
    source_repo: Path,
    worktrees_dir: Path,
) -> None:
    """retire() refuses when the worktree has staged (but uncommitted) changes."""
    job_create = _make_job("g2-staged-001")
    worktree = await lifecycle.create(job_create)

    staged_file = worktree / "staged.txt"
    staged_file.write_text("staged but not committed\n")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "staged.txt"],
        check=True,
    )

    job_landed = _make_job("g2-staged-001", status="landed")
    with pytest.raises(WorktreeRetireError, match="uncommitted changes"):
        await lifecycle.retire(job_landed, worktree)

    assert worktree.is_dir()


async def test_retire_clean_worktree_succeeds(
    lifecycle: GitWorktreeLifecycle,
    source_repo: Path,
    worktrees_dir: Path,
) -> None:
    """retire() on a clean landed worktree removes it without raising."""
    job_create = _make_job("g2-clean-001")
    worktree = await lifecycle.create(job_create)
    assert worktree.is_dir()

    job_landed = _make_job("g2-clean-001", status="landed")
    await lifecycle.retire(job_landed, worktree)

    assert not worktree.exists()


# ---------------------------------------------------------------------------
# Windows file-lock retry simulation
# ---------------------------------------------------------------------------


async def test_retire_retries_on_file_lock(
    lifecycle: GitWorktreeLifecycle,
    source_repo: Path,
    worktrees_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retire() retries git worktree remove on failure and succeeds on 3rd attempt."""
    job_create = _make_job("g2-retry-001")
    worktree = await lifecycle.create(job_create)

    # Capture how many times _run_git is called for "worktree remove".
    call_count = 0
    original_run_git = lifecycle.__class__.__bases__[0]  # just for reference

    import claude_fleet.worktree as wt_mod  # noqa: PLC0415

    real_run_git = wt_mod._run_git
    attempts: list[int] = []

    async def flaky_run_git(*args: str, cwd: Path | None = None) -> None:
        nonlocal call_count
        if args[:2] == ("worktree", "remove"):
            call_count += 1
            attempts.append(call_count)
            if call_count < 3:
                raise RuntimeError(f"file locked (attempt {call_count})")
        await real_run_git(*args, cwd=cwd)

    monkeypatch.setattr(wt_mod, "_run_git", flaky_run_git)
    # Also patch asyncio.sleep to avoid actually sleeping in tests.
    sleep_calls: list[float] = []

    async def fast_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    job_landed = _make_job("g2-retry-001", status="landed")
    await lifecycle.retire(job_landed, worktree)

    assert call_count == 3, f"expected 3 attempts, got {call_count}"
    assert len(sleep_calls) == 2, f"expected 2 sleeps (between retries), got {sleep_calls}"
    assert all(s == 1.0 for s in sleep_calls)
    # Worktree is gone after success.
    assert not worktree.exists()


async def test_retire_raises_after_all_retries_exhausted(
    lifecycle: GitWorktreeLifecycle,
    source_repo: Path,
    worktrees_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retire() re-raises the RuntimeError when all 3 retries are exhausted."""
    job_create = _make_job("g2-exhaust-001")
    worktree = await lifecycle.create(job_create)

    import claude_fleet.worktree as wt_mod  # noqa: PLC0415

    async def always_fail(*args: str, cwd: Path | None = None) -> None:
        if args[:2] == ("worktree", "remove"):
            raise RuntimeError("always locked")
        await wt_mod._run_git(*args, cwd=cwd)

    monkeypatch.setattr(wt_mod, "_run_git", always_fail)

    async def fast_sleep(secs: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    job_landed = _make_job("g2-exhaust-001", status="landed")
    with pytest.raises(RuntimeError, match="always locked"):
        await lifecycle.retire(job_landed, worktree)


# ---------------------------------------------------------------------------
# Daemon wiring tests
# ---------------------------------------------------------------------------


class FakeWorkflowLifecycle:
    """Minimal WorktreeLifecycle stub that records retire() calls."""

    def __init__(self) -> None:
        self.retire_calls: list[tuple[str, Path]] = []
        self.should_raise: Exception | None = None

    async def create(self, job: Job) -> Path:
        return Path(f"/tmp/worktrees/{job.id}")

    async def retire(self, job: Job, worktree: Path) -> None:
        if self.should_raise is not None:
            raise self.should_raise
        self.retire_calls.append((job.id, worktree))

    async def exists(self, job: Job) -> Path | None:
        return None


class FakeLandBackend:
    """Scripted LandBackend for daemon wiring tests."""

    def __init__(self, outcome: str = "ok", commit: str = "abc123") -> None:
        self._outcome = outcome
        self._commit = commit
        self.spawn_calls: list[str] = []
        self._handles: dict[str, object] = {}

    async def spawn_land(self, job: Job) -> object:
        from claude_fleet.orchestrator.land_events import LandHandle, LandResult  # noqa: PLC0415

        self.spawn_calls.append(job.id)
        result = LandResult(
            outcome=self._outcome,  # type: ignore[arg-type]
            attempt=job.land_attempts,
            commit=self._commit if self._outcome == "ok" else None,
        )

        async def _immediate() -> LandResult:
            return result

        task: asyncio.Task[LandResult] = asyncio.create_task(_immediate())
        handle = LandHandle(job_id=job.id, attempt=job.land_attempts, task=task)
        return handle

    async def harvest(self, handle: object) -> object:
        from claude_fleet.orchestrator.land_events import LandHandle  # noqa: PLC0415

        assert isinstance(handle, LandHandle)
        if handle.task.done():
            return handle.task.result()
        return None

    async def terminate(self, handle: object) -> None:
        pass


async def _run_daemon_ticks(daemon: object, ticks: int) -> None:
    """Run the daemon for a fixed number of ticks then stop."""
    import asyncio  # noqa: PLC0415
    from claude_fleet.orchestrator.daemon import Daemon  # noqa: PLC0415

    assert isinstance(daemon, Daemon)
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    try:
        await asyncio.sleep(daemon.tick_interval_s * (ticks + 0.5))
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_daemon_calls_lifecycle_retire_on_landed(tmp_path: Path) -> None:
    """When auto_retire=True and job transitions to landed, lifecycle.retire() is called."""
    from claude_fleet.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415
    from claude_fleet.orchestrator.daemon import Daemon  # noqa: PLC0415
    from tests.unit.orchestrator.fakes import FakeBackend, FakeSpec  # noqa: PLC0415

    db = tmp_path / "daemon_retire.db"
    orch = Orchestrator(db, lease_ttl_s=600)
    backend = FakeBackend()
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir()
    fake_lifecycle = FakeWorkflowLifecycle()
    fake_land = FakeLandBackend(outcome="ok", commit="cafecafe")

    # Enqueue and bring the job to audited+PASS so the daemon can land it.
    # We use direct SQL mutation (same pattern as test_orchestrator_land_states.py)
    # to bypass the full audit-loop state machine; the daemon only needs the job
    # in audited+PASS to exercise the land → landed → retire path.
    orch.enqueue("job-land-A", "/tmp/job-land-A.md")
    with orch._conn_lock:  # noqa: SLF001
        orch._conn.execute(  # noqa: SLF001
            "UPDATE jobs SET status='audited', audit_verdict='PASS' WHERE id=?",
            ("job-land-A",),
        )

    daemon = Daemon(
        orch,
        backend,
        tick_interval_s=0.05,
        max_concurrent=4,
        auto_land=True,
        land_backend=fake_land,
        auto_retire=True,
        lifecycle=fake_lifecycle,
        worktree_base=worktree_base,
        retire_only_after_landed=True,
    )

    try:
        await _run_daemon_ticks(daemon, ticks=8)
    finally:
        orch.close()

    # Job must have landed.
    assert len(fake_land.spawn_calls) == 1
    # lifecycle.retire() must have been called once for that job.
    assert len(fake_lifecycle.retire_calls) == 1
    job_id_retired, wt_path = fake_lifecycle.retire_calls[0]
    assert job_id_retired == "job-land-A"
    assert wt_path == worktree_base / "job-land-A"


async def test_daemon_does_not_call_lifecycle_retire_when_disabled(
    tmp_path: Path,
) -> None:
    """When auto_retire=False, lifecycle.retire() is never called on landed."""
    from claude_fleet.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415
    from claude_fleet.orchestrator.daemon import Daemon  # noqa: PLC0415
    from tests.unit.orchestrator.fakes import FakeBackend  # noqa: PLC0415

    db = tmp_path / "daemon_noretire.db"
    orch = Orchestrator(db, lease_ttl_s=600)
    backend = FakeBackend()
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir()
    fake_lifecycle = FakeWorkflowLifecycle()
    fake_land = FakeLandBackend(outcome="ok", commit="deadbeef")

    orch.enqueue("job-land-B", "/tmp/job-land-B.md")
    with orch._conn_lock:  # noqa: SLF001
        orch._conn.execute(  # noqa: SLF001
            "UPDATE jobs SET status='audited', audit_verdict='PASS' WHERE id=?",
            ("job-land-B",),
        )

    daemon = Daemon(
        orch,
        backend,
        tick_interval_s=0.05,
        max_concurrent=4,
        auto_land=True,
        land_backend=fake_land,
        auto_retire=False,  # <-- disabled
        lifecycle=fake_lifecycle,
        worktree_base=worktree_base,
        retire_only_after_landed=True,
    )

    try:
        await _run_daemon_ticks(daemon, ticks=8)
    finally:
        orch.close()

    # No retire calls — auto_retire is False.
    assert fake_lifecycle.retire_calls == []


async def test_daemon_lifecycle_retire_failure_does_not_crash_daemon(
    tmp_path: Path,
) -> None:
    """If lifecycle.retire() raises, the daemon logs a warning and keeps running."""
    from claude_fleet.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415
    from claude_fleet.orchestrator.daemon import Daemon  # noqa: PLC0415
    from tests.unit.orchestrator.fakes import FakeBackend  # noqa: PLC0415

    db = tmp_path / "daemon_retire_fail.db"
    orch = Orchestrator(db, lease_ttl_s=600)
    backend = FakeBackend()
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir()
    fake_lifecycle = FakeWorkflowLifecycle()
    fake_lifecycle.should_raise = WorktreeRetireError("uncommitted changes in worktree")
    fake_land = FakeLandBackend(outcome="ok", commit="00000001")

    orch.enqueue("job-land-C", "/tmp/job-land-C.md")
    with orch._conn_lock:  # noqa: SLF001
        orch._conn.execute(  # noqa: SLF001
            "UPDATE jobs SET status='audited', audit_verdict='PASS' WHERE id=?",
            ("job-land-C",),
        )

    daemon = Daemon(
        orch,
        backend,
        tick_interval_s=0.05,
        max_concurrent=4,
        auto_land=True,
        land_backend=fake_land,
        auto_retire=True,
        lifecycle=fake_lifecycle,
        worktree_base=worktree_base,
        retire_only_after_landed=True,
    )

    # Must not raise even though retire() raises.
    await _run_daemon_ticks(daemon, ticks=8)

    # Job still landed — retire failure should not affect job status.
    final = orch.status("job-land-C")
    assert final.status == "landed"
    orch.close()


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_worktree_retire_happy_path(
    tmp_path: Path,
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
) -> None:
    """CLI ``worktree retire <id>`` happy path: landed job, clean worktree."""
    from claude_fleet.cli import app  # noqa: PLC0415
    from claude_fleet.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415
    from claude_fleet.orchestrator.config import OrchestratorConfig  # noqa: PLC0415

    db = tmp_path / "cli_retire.db"
    cfg = OrchestratorConfig(db_path=db)
    orch = Orchestrator(cfg.db_path, lease_ttl_s=cfg.lease_ttl_s)
    try:
        # Create a job and fast-path it to landed via direct SQL.
        orch.enqueue("cli-retire-01", "/tmp/cli-retire-01.md")
        with orch._conn_lock:  # noqa: SLF001
            orch._conn.execute(  # noqa: SLF001
                "UPDATE jobs SET status='audited', audit_verdict='PASS' WHERE id=?",
                ("cli-retire-01",),
            )
        orch.begin_landing("cli-retire-01")
        orch.mark_landed("cli-retire-01", "abc000")
    finally:
        orch.close()

    # Create the actual worktree on disk.
    job_for_create = _make_job("cli-retire-01")
    worktree = asyncio.run(lifecycle.create(job_for_create))
    assert worktree.is_dir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "worktree",
            "retire",
            "--worktree-base",
            str(worktrees_dir),
            "--repo-root",
            str(source_repo),
            "cli-retire-01",
        ],
    )
    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert not worktree.exists(), "worktree should be removed"


def test_cli_worktree_retire_non_landed_job_exits_1(tmp_path: Path) -> None:
    """CLI ``worktree retire <id>`` on a non-landed job exits with code 1."""
    from claude_fleet.cli import app  # noqa: PLC0415
    from claude_fleet.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415
    from claude_fleet.orchestrator.config import OrchestratorConfig  # noqa: PLC0415

    db = tmp_path / "cli_retire_nonlanded.db"
    cfg = OrchestratorConfig(db_path=db)
    orch = Orchestrator(cfg.db_path, lease_ttl_s=cfg.lease_ttl_s)
    try:
        orch.enqueue("cli-pending-01", "/tmp/cli-pending-01.md")
        # Leave job in pending status.
    finally:
        orch.close()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "worktree",
            "retire",
            "cli-pending-01",
        ],
    )
    assert result.exit_code == 1
    assert "not landed" in result.output.lower() or "not landed" in (result.stderr or "").lower()
