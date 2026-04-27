"""G8 worktree sweep tests.

Covers per-spec §G8:
- Orphan with no jobs row → swept
- Active job (status=running) → not swept
- Recently-landed job (within orphan_age) → not swept
- Old landed job (past orphan_age) → swept
- --dry-run → no FS changes

CLI tests:
- Happy path (orphan removed)
- --dry-run (listed, not removed)

All tests use a real git repository for worktree mechanics, consistent
with G1/G2 test patterns.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from rookery.orchestrator.backend import Job
from rookery.worktree import GitWorktreeLifecycle, OrphanInfo, find_orphans


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_job(job_id: str, status: str = "pending") -> Job:
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


# ---------------------------------------------------------------------------
# Session-scoped source repo fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def source_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Minimal git repo for G8 integration tests."""
    repo = tmp_path_factory.mktemp("g8_source_repo")
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
# Helper: create a minimal SQLite DB with a jobs row
# ---------------------------------------------------------------------------


def _make_db(db_path: Path) -> None:
    """Create the minimal jobs table schema used by find_orphans."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_job(db_path: Path, job_id: str, status: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO jobs (id, status) VALUES (?, ?)", (job_id, status))
    conn.commit()
    conn.close()


def _set_mtime_old(path: Path, age_hours: float = 200.0) -> None:
    """Backdate a path's mtime by *age_hours* hours."""
    old_ts = time.time() - age_hours * 3600.0
    import os
    os.utime(str(path), (old_ts, old_ts))


# ---------------------------------------------------------------------------
# find_orphans() unit tests
# ---------------------------------------------------------------------------


async def test_no_jobs_row_is_orphaned(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
    tmp_path: Path,
) -> None:
    """Worktree with no matching jobs row is always an orphan."""
    db = tmp_path / "sweep.db"
    _make_db(db)

    job = _make_job("sweep-no-row-001")
    await lifecycle.create(job)
    wt = worktrees_dir / "sweep-no-row-001"
    assert wt.is_dir()

    orphans = await find_orphans(worktrees_dir, db, orphan_age_hours=168.0)
    assert len(orphans) == 1
    assert orphans[0].reason == "no jobs row"
    assert orphans[0].path == wt.resolve()


async def test_running_job_not_swept(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
    tmp_path: Path,
) -> None:
    """Worktree for a running job (active, non-terminal) is never swept."""
    db = tmp_path / "sweep.db"
    _make_db(db)
    _insert_job(db, "sweep-running-001", "running")

    job = _make_job("sweep-running-001", status="running")
    await lifecycle.create(job)
    wt = worktrees_dir / "sweep-running-001"
    assert wt.is_dir()

    # Even with a very short age threshold, running jobs are exempt.
    orphans = await find_orphans(worktrees_dir, db, orphan_age_hours=0.0)
    assert orphans == []


async def test_pending_job_not_swept(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
    tmp_path: Path,
) -> None:
    """Worktree for a pending job is never swept (non-terminal status)."""
    db = tmp_path / "sweep.db"
    _make_db(db)
    _insert_job(db, "sweep-pending-001", "pending")

    job = _make_job("sweep-pending-001")
    await lifecycle.create(job)

    orphans = await find_orphans(worktrees_dir, db, orphan_age_hours=0.0)
    assert orphans == []


async def test_recently_landed_job_not_swept(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
    tmp_path: Path,
) -> None:
    """Landed worktree within the age threshold is NOT swept."""
    db = tmp_path / "sweep.db"
    _make_db(db)
    _insert_job(db, "sweep-recent-001", "landed")

    job = _make_job("sweep-recent-001", status="landed")
    await lifecycle.create(job)
    wt = worktrees_dir / "sweep-recent-001"
    assert wt.is_dir()
    # mtime is recent (just created) — should be within the 168 h threshold.

    orphans = await find_orphans(worktrees_dir, db, orphan_age_hours=168.0)
    assert orphans == [], "recently landed worktree must not be swept"


async def test_old_landed_job_is_swept(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
    tmp_path: Path,
) -> None:
    """Landed worktree older than the age threshold IS swept."""
    db = tmp_path / "sweep.db"
    _make_db(db)
    _insert_job(db, "sweep-old-001", "landed")

    job = _make_job("sweep-old-001", status="landed")
    await lifecycle.create(job)
    wt = worktrees_dir / "sweep-old-001"
    assert wt.is_dir()

    # Backdate mtime to 200 hours ago (past the 168 h default).
    _set_mtime_old(wt, age_hours=200.0)

    orphans = await find_orphans(worktrees_dir, db, orphan_age_hours=168.0)
    assert len(orphans) == 1
    assert "landed" in orphans[0].reason


async def test_old_failed_job_is_swept(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
    tmp_path: Path,
) -> None:
    """Failed worktree older than the age threshold IS swept."""
    db = tmp_path / "sweep.db"
    _make_db(db)
    _insert_job(db, "sweep-failed-001", "failed")

    job = _make_job("sweep-failed-001", status="failed")
    await lifecycle.create(job)
    wt = worktrees_dir / "sweep-failed-001"
    _set_mtime_old(wt, age_hours=200.0)

    orphans = await find_orphans(worktrees_dir, db, orphan_age_hours=168.0)
    assert len(orphans) == 1
    assert "failed" in orphans[0].reason


async def test_empty_worktree_base_returns_empty_list(tmp_path: Path) -> None:
    """find_orphans returns [] when worktree_base has no sub-directories."""
    db = tmp_path / "sweep.db"
    _make_db(db)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    orphans = await find_orphans(wt_base, db, orphan_age_hours=168.0)
    assert orphans == []


async def test_nonexistent_worktree_base_returns_empty_list(tmp_path: Path) -> None:
    """find_orphans returns [] when worktree_base doesn't exist."""
    db = tmp_path / "sweep.db"
    _make_db(db)
    missing = tmp_path / "does_not_exist"

    orphans = await find_orphans(missing, db, orphan_age_hours=168.0)
    assert orphans == []


async def test_mixed_scenario(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
    tmp_path: Path,
) -> None:
    """Mixed scenario: one orphan, one active, one recent-landed, one old-landed."""
    db = tmp_path / "sweep.db"
    _make_db(db)

    # 1. Orphan with no row.
    job_orphan = _make_job("sweep-mix-orphan")
    await lifecycle.create(job_orphan)

    # 2. Running job — should NOT be swept.
    _insert_job(db, "sweep-mix-running", "running")
    job_running = _make_job("sweep-mix-running", status="running")
    await lifecycle.create(job_running)

    # 3. Recently landed — should NOT be swept.
    _insert_job(db, "sweep-mix-recent", "landed")
    job_recent = _make_job("sweep-mix-recent", status="landed")
    await lifecycle.create(job_recent)

    # 4. Old landed — should be swept.
    _insert_job(db, "sweep-mix-old", "landed")
    job_old = _make_job("sweep-mix-old", status="landed")
    await lifecycle.create(job_old)
    _set_mtime_old(worktrees_dir / "sweep-mix-old", age_hours=200.0)

    orphans = await find_orphans(worktrees_dir, db, orphan_age_hours=168.0)
    orphan_names = {o.path.name for o in orphans}
    assert orphan_names == {"sweep-mix-orphan", "sweep-mix-old"}


# ---------------------------------------------------------------------------
# force_remove() tests
# ---------------------------------------------------------------------------


async def test_force_remove_removes_worktree(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
) -> None:
    """force_remove() removes the worktree directory regardless of job status."""
    job = _make_job("sweep-force-001")
    worktree = await lifecycle.create(job)
    assert worktree.is_dir()

    await lifecycle.force_remove(worktree)
    assert not worktree.exists()


async def test_force_remove_on_dirty_worktree(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
) -> None:
    """force_remove() succeeds even when worktree has uncommitted changes."""
    job = _make_job("sweep-force-dirty-001")
    worktree = await lifecycle.create(job)
    (worktree / "dirty.txt").write_text("untracked\n")

    # force_remove should not refuse on dirty state (unlike retire()).
    await lifecycle.force_remove(worktree)
    assert not worktree.exists()


# ---------------------------------------------------------------------------
# --dry-run CLI test
# ---------------------------------------------------------------------------


def test_cli_sweep_dry_run_no_fs_changes(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
    tmp_path: Path,
) -> None:
    """CLI --dry-run lists orphans without removing them."""
    from rookery.cli import app  # noqa: PLC0415

    db = tmp_path / "sweep_cli.db"
    _make_db(db)
    # Create an orphan worktree (no jobs row).
    job = _make_job("sweep-dryrun-001")
    asyncio.run(lifecycle.create(job))
    wt = worktrees_dir / "sweep-dryrun-001"
    assert wt.is_dir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "worktree",
            "sweep",
            "--dry-run",
            "--worktree-base",
            str(worktrees_dir),
            "--repo-root",
            str(source_repo),
        ],
    )
    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    # Worktree must still exist — dry-run must not remove it.
    assert wt.is_dir(), "dry-run must not remove worktrees"
    assert "dry-run" in result.output.lower() or "no orphan" in result.output.lower() or "found" in result.output.lower()


def test_cli_sweep_removes_orphan(
    source_repo: Path,
    worktrees_dir: Path,
    lifecycle: GitWorktreeLifecycle,
    tmp_path: Path,
) -> None:
    """CLI sweep (no --dry-run) removes an orphan worktree."""
    from rookery.cli import app  # noqa: PLC0415

    db = tmp_path / "sweep_cli_remove.db"
    _make_db(db)
    # Create an orphan worktree (no jobs row).
    job = _make_job("sweep-remove-001")
    asyncio.run(lifecycle.create(job))
    wt = worktrees_dir / "sweep-remove-001"
    assert wt.is_dir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "worktree",
            "sweep",
            "--worktree-base",
            str(worktrees_dir),
            "--repo-root",
            str(source_repo),
        ],
    )
    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert not wt.exists(), "sweep must remove the orphan worktree"
    assert "removed" in result.output.lower()


def test_cli_sweep_no_orphans_exit_0(
    tmp_path: Path,
) -> None:
    """CLI sweep exits 0 with a clean message when no orphans are found."""
    from rookery.cli import app  # noqa: PLC0415

    db = tmp_path / "sweep_none.db"
    _make_db(db)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "worktree",
            "sweep",
            "--worktree-base",
            str(wt_base),
        ],
    )
    assert result.exit_code == 0
    assert "no orphaned" in result.output.lower()
