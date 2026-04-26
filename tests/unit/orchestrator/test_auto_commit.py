"""Tests for the auto-commit-on-PASS harvest hook.

Each test uses a real (but minimal) git repo in tmp_path so the git
subprocess calls go to actual on-disk objects, letting us verify branch
HEAD advances (or doesn't) as expected.

Five cases:
1. PASS verdict + dirty worktree  → auto-commits, branch HEAD advances.
2. PASS verdict + clean worktree  → no-op (worker committed themselves).
3. PASS verdict + commit fails    → logs error, status still transitions to done.
4. auto_commit_on_pass=False      → no commit, branch unchanged.
5. BLOCK verdict + dirty worktree → no commit even if worktree dirty.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from claude_fleet.orchestrator import Orchestrator
from claude_fleet.orchestrator.backend import Job, OrchestratorBackend, WorkerHandle
from claude_fleet.orchestrator.daemon import Daemon

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASS_MARKER = """\
# Parcel Done

**Verdict:** PASS

## Summary

Implement gitstats feature

## Details

All changes committed.
"""

_BLOCK_MARKER = """\
# Parcel Done

**Verdict:** BLOCK

## Summary

Tests failed; blocking.
"""


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_git_repo(path: Path) -> Path:
    """Initialise a minimal git repo in *path*, returning *path*."""
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@cf.test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "CF Test"],
        check=True,
        capture_output=True,
    )
    # Initial commit so HEAD exists.
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "chore: init"],
        check=True,
        capture_output=True,
    )
    return path


def _head_sha(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# Fake backend that controls what harvest() returns per job.
# ---------------------------------------------------------------------------


class _ScriptedBackend(OrchestratorBackend):
    """Minimal backend that returns a pre-scripted harvest result.

    *results* maps job_id → dict that harvest() returns (or None to indicate
    'still running').  worktree_map maps job_id → worktree Path.
    """

    def __init__(
        self,
        results: dict[str, dict[str, Any] | None],
        worktree_map: dict[str, Path],
    ) -> None:
        self._results = results
        self._worktree_map = worktree_map
        self._harvested: set[str] = set()

    async def spawn(self, job: Job) -> WorkerHandle:
        return WorkerHandle(
            job_id=job.id,
            worker_id=f"scripted-{job.id}",
            pid=-1,
            worktree=self._worktree_map[job.id],
            log_path=Path("/dev/null"),
        )

    async def is_alive(self, handle: WorkerHandle) -> bool:
        return handle.job_id not in self._harvested

    async def harvest(self, handle: WorkerHandle) -> dict[str, Any] | None:
        result = self._results.get(handle.job_id)
        if result is not None:
            self._harvested.add(handle.job_id)
        return result

    async def terminate(self, handle: WorkerHandle) -> None:
        pass


# ---------------------------------------------------------------------------
# Helper to run the daemon for a fixed number of ticks
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402 — after class definitions for readability


async def _run_ticks(daemon: Daemon, ticks: int = 6) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    try:
        await asyncio.sleep(daemon.tick_interval_s * (ticks + 0.5))
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_pass_verdict_dirty_worktree_auto_commits(tmp_path: Path) -> None:
    """PASS verdict + unstaged file → daemon creates a commit, HEAD advances."""
    wt = _make_git_repo(tmp_path / "wt-J1")
    sha_before = _head_sha(wt)

    # Worker left a file but did not commit.
    (wt / "output.py").write_text("# generated\n")

    result = {"status": "done", "parcel_done_md": _PASS_MARKER}
    backend = _ScriptedBackend({"J1": result}, {"J1": wt})

    db = tmp_path / "q.db"
    orch = Orchestrator(db, lease_ttl_s=600)
    daemon = Daemon(orch, backend, tick_interval_s=0.05, max_concurrent=2, auto_commit_on_pass=True)
    orch.enqueue("J1", "/tmp/J1.md")

    await _run_ticks(daemon)

    job = orch.status("J1")
    assert job.status == "done"
    sha_after = _head_sha(wt)
    assert sha_after != sha_before, "Expected a new commit on the parcel branch"

    # Commit message should contain the job id and summary line.
    log_msg = _git(wt, "log", "-1", "--pretty=%s").stdout.strip()
    assert "J1" in log_msg
    assert "gitstats" in log_msg.lower() or "claude-fleet" in log_msg.lower()

    orch.close()


async def test_pass_verdict_clean_worktree_no_commit(tmp_path: Path) -> None:
    """PASS verdict + already-committed worktree → no duplicate commit."""
    wt = _make_git_repo(tmp_path / "wt-J2")

    # Worker committed their own output.
    (wt / "output.py").write_text("# worker self-commit\n")
    _git(wt, "add", "output.py")
    _git(wt, "commit", "-m", "feat: worker self-committed")
    sha_before = _head_sha(wt)

    result = {"status": "done", "parcel_done_md": _PASS_MARKER}
    backend = _ScriptedBackend({"J2": result}, {"J2": wt})

    db = tmp_path / "q.db"
    orch = Orchestrator(db, lease_ttl_s=600)
    daemon = Daemon(orch, backend, tick_interval_s=0.05, max_concurrent=2, auto_commit_on_pass=True)
    orch.enqueue("J2", "/tmp/J2.md")

    await _run_ticks(daemon)

    job = orch.status("J2")
    assert job.status == "done"
    sha_after = _head_sha(wt)
    assert sha_after == sha_before, "No new commit expected on a clean worktree"

    orch.close()


async def test_pass_verdict_commit_fails_still_transitions(tmp_path: Path) -> None:
    """Git commit failure → last_error recorded, job still transitions to done."""
    # Point worktree at a non-existent path so git fails.
    wt = tmp_path / "no-such-dir"

    result = {"status": "done", "parcel_done_md": _PASS_MARKER}
    backend = _ScriptedBackend({"J3": result}, {"J3": wt})

    db = tmp_path / "q.db"
    orch = Orchestrator(db, lease_ttl_s=600)
    daemon = Daemon(orch, backend, tick_interval_s=0.05, max_concurrent=2, auto_commit_on_pass=True)
    orch.enqueue("J3", "/tmp/J3.md")

    await _run_ticks(daemon)

    job = orch.status("J3")
    # Verdict transition must NOT be blocked.
    assert job.status == "done"
    # Error should have been recorded.
    assert job.last_error is not None
    assert "auto-commit failed" in job.last_error

    orch.close()


async def test_auto_commit_on_pass_false_no_commit(tmp_path: Path) -> None:
    """auto_commit_on_pass=False → no commit even if worktree is dirty."""
    wt = _make_git_repo(tmp_path / "wt-J4")
    sha_before = _head_sha(wt)

    (wt / "output.py").write_text("# should not be committed\n")

    result = {"status": "done", "parcel_done_md": _PASS_MARKER}
    backend = _ScriptedBackend({"J4": result}, {"J4": wt})

    db = tmp_path / "q.db"
    orch = Orchestrator(db, lease_ttl_s=600)
    daemon = Daemon(
        orch, backend, tick_interval_s=0.05, max_concurrent=2, auto_commit_on_pass=False
    )
    orch.enqueue("J4", "/tmp/J4.md")

    await _run_ticks(daemon)

    job = orch.status("J4")
    assert job.status == "done"
    sha_after = _head_sha(wt)
    assert sha_after == sha_before, "No commit expected when auto_commit_on_pass=False"

    orch.close()


async def test_block_verdict_dirty_worktree_no_commit(tmp_path: Path) -> None:
    """BLOCK verdict + dirty worktree → no commit, job transitions normally."""
    wt = _make_git_repo(tmp_path / "wt-J5")
    sha_before = _head_sha(wt)

    (wt / "output.py").write_text("# should not be committed\n")

    result = {"status": "done", "parcel_done_md": _BLOCK_MARKER}
    backend = _ScriptedBackend({"J5": result}, {"J5": wt})

    db = tmp_path / "q.db"
    orch = Orchestrator(db, lease_ttl_s=600)
    daemon = Daemon(orch, backend, tick_interval_s=0.05, max_concurrent=2, auto_commit_on_pass=True)
    orch.enqueue("J5", "/tmp/J5.md")

    await _run_ticks(daemon)

    job = orch.status("J5")
    assert job.status == "done"
    sha_after = _head_sha(wt)
    assert sha_after == sha_before, "No commit expected for non-PASS verdict"

    orch.close()
