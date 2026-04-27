"""Unit tests for ``rookery.worktree``.

All tests use a real git repository initialised in ``tmp_path`` so that
``git worktree add`` exercises genuine git mechanics rather than mocked
subprocess calls.

Fixtures
--------
source_repo (session-scoped)
    A bare-minimum git repo with one commit on ``main``.  Shared across
    all tests to avoid re-initialising git on every test.

worktrees_dir (function-scoped)
    A fresh sub-directory of ``tmp_path`` used as ``GitWorktreeLifecycle.base_dir``.

lifecycle (function-scoped)
    A ``GitWorktreeLifecycle`` instance pointed at ``source_repo`` and
    ``worktrees_dir``.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from rookery.orchestrator.backend import Job
from rookery.worktree import GitWorktreeLifecycle, WorktreeLifecycle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(job_id: str, status: str = "pending") -> Job:
    """Create a minimal :class:`Job` with *status* for testing."""
    return Job(
        id=job_id,
        prompt_path=f"parcels/{job_id}.md",
        status=status,  # type: ignore[arg-type]
    )


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo*; return stdout stripped. Raises on failure."""
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
    """Initialise a minimal git repo with one commit on ``main``.

    Session-scoped so we only do this once across all tests in this file.
    """
    repo = tmp_path_factory.mktemp("source_repo")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


# ---------------------------------------------------------------------------
# Function-scoped lifecycle fixture
# ---------------------------------------------------------------------------


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
# Tests
# ---------------------------------------------------------------------------


async def test_create_worktree_exists_at_expected_path(
    lifecycle: GitWorktreeLifecycle,
    worktrees_dir: Path,
) -> None:
    """create() places the worktree at base_dir / job.id."""
    job = _make_job("job-001")
    result = await lifecycle.create(job)

    expected = (worktrees_dir / "job-001").resolve()
    assert result == expected
    assert result.is_dir(), f"worktree directory not found at {result}"


async def test_create_worktree_creates_branch(
    lifecycle: GitWorktreeLifecycle,
    source_repo: Path,
) -> None:
    """create() creates a branch named parcel/<job_id> in the source repo."""
    job = _make_job("job-002")
    await lifecycle.create(job)

    branches = _git(source_repo, "branch", "--list", "parcel/job-002")
    assert "parcel/job-002" in branches


async def test_create_idempotent_returns_existing_path(
    lifecycle: GitWorktreeLifecycle,
    worktrees_dir: Path,
) -> None:
    """create() called twice returns the same path without raising."""
    job = _make_job("job-003")
    path1 = await lifecycle.create(job)
    path2 = await lifecycle.create(job)  # second call — worktree already on disk

    assert path1 == path2
    assert path2.is_dir()


async def test_exists_returns_path_when_present(
    lifecycle: GitWorktreeLifecycle,
) -> None:
    """exists() returns the path after create() has been called."""
    job = _make_job("job-004")
    await lifecycle.create(job)

    found = await lifecycle.exists(job)
    assert found is not None
    assert found.is_dir()


async def test_exists_returns_none_when_absent(
    lifecycle: GitWorktreeLifecycle,
) -> None:
    """exists() returns None for a job whose worktree has never been created."""
    job = _make_job("job-no-such")
    assert await lifecycle.exists(job) is None


async def test_retire_on_non_landed_raises_value_error(
    lifecycle: GitWorktreeLifecycle,
    worktrees_dir: Path,
) -> None:
    """retire() raises ValueError when job.status != 'landed'."""
    job_running = _make_job("job-005", status="running")
    fake_worktree = worktrees_dir / "job-005"
    fake_worktree.mkdir()

    with pytest.raises(ValueError, match="non-landed"):
        await lifecycle.retire(job_running, fake_worktree)


async def test_retire_on_non_landed_pending_raises(
    lifecycle: GitWorktreeLifecycle,
    worktrees_dir: Path,
) -> None:
    """retire() raises ValueError for status=pending as well."""
    job = _make_job("job-006", status="pending")
    fake_worktree = worktrees_dir / "job-006"
    fake_worktree.mkdir()

    with pytest.raises(ValueError):
        await lifecycle.retire(job, fake_worktree)


async def test_retire_landed_removes_worktree_and_branch(
    lifecycle: GitWorktreeLifecycle,
    source_repo: Path,
    worktrees_dir: Path,
) -> None:
    """retire() on a landed job removes the worktree directory and branch."""
    job_create = _make_job("job-007")
    worktree = await lifecycle.create(job_create)
    assert worktree.is_dir()

    # Simulate landing: the job must have status=landed for retire to proceed.
    job_landed = _make_job("job-007", status="landed")
    await lifecycle.retire(job_landed, worktree)

    assert not worktree.exists(), "worktree directory should be gone after retire()"
    branches = _git(source_repo, "branch", "--list", "parcel/job-007")
    assert branches == "", f"branch should be deleted, found: {branches!r}"


async def test_corrupted_state_orphan_branch_no_dir(
    lifecycle: GitWorktreeLifecycle,
    source_repo: Path,
    worktrees_dir: Path,
) -> None:
    """create() recovers when a parcel branch exists but the worktree dir does not.

    This models a crash that left the branch behind but never created (or
    deleted) the worktree directory.  The expected behaviour: git worktree add
    will fail because the branch already exists.  We verify the error surfaces
    as RuntimeError with useful context rather than silently doing the wrong
    thing.
    """
    # Manually create an orphan branch in the repo (simulating partial prev run).
    _git(source_repo, "branch", "parcel/job-orphan", "main")

    job = _make_job("job-orphan")
    # The worktree dir doesn't exist, but the branch does.
    assert not (worktrees_dir / "job-orphan").exists()

    with pytest.raises(RuntimeError, match="git worktree add failed|already exists"):
        await lifecycle.create(job)


# ---------------------------------------------------------------------------
# ABC compliance
# ---------------------------------------------------------------------------


def test_worktree_lifecycle_is_abc() -> None:
    """WorktreeLifecycle cannot be instantiated directly."""
    with pytest.raises(TypeError):
        WorktreeLifecycle()  # type: ignore[abstract]


def test_git_worktree_lifecycle_is_subclass() -> None:
    """GitWorktreeLifecycle is a subclass of WorktreeLifecycle."""
    assert issubclass(GitWorktreeLifecycle, WorktreeLifecycle)
