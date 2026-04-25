"""WorktreeLifecycle: create, query, and retire git worktrees for parcel jobs.

This module provides a first-class lifecycle abstraction so the orchestrator
daemon can manage worktrees without requiring external shell scripts.

Classes
-------
WorktreeLifecycle
    ABC defining the three lifecycle operations.
GitWorktreeLifecycle
    Default implementation backed by ``git worktree`` sub-commands, invoked
    via :mod:`asyncio.create_subprocess_exec` (no ``shell=True``).

Design notes
------------
* ``create()`` is idempotent — if the worktree directory already exists
  (e.g. after a daemon crash-restart) it is reused without calling
  ``git worktree add`` again.
* ``retire()`` raises :exc:`ValueError` for non-landed jobs to prevent
  accidental data loss; the caller must gate on ``job.status == "landed"``.
* ``exists()`` is a lightweight stat check, not a git-porcelain call.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path

from claude_fleet.orchestrator.backend import Job


class WorktreeLifecycle(ABC):
    """Abstract lifecycle manager for parcel worktrees."""

    @abstractmethod
    async def create(self, job: Job) -> Path:
        """Create a worktree for *job*; return its absolute path.

        Must be idempotent: if a worktree for this job already exists
        (identified by ``job.id``), return its path without error.
        """

    @abstractmethod
    async def retire(self, job: Job, worktree: Path) -> None:
        """Remove *worktree* and delete its branch.

        Parameters
        ----------
        job:
            Must have ``status == "landed"``; raises :exc:`ValueError` otherwise.
        worktree:
            Absolute path to the worktree directory.

        Raises
        ------
        ValueError
            When ``job.status != "landed"``.
        """

    @abstractmethod
    async def exists(self, job: Job) -> Path | None:
        """Return the worktree path if it exists, ``None`` otherwise."""


async def _run_git(*args: str, cwd: Path | None = None) -> None:
    """Run ``git <args>`` via :func:`asyncio.create_subprocess_exec`.

    Parameters
    ----------
    *args:
        Arguments forwarded to git (e.g. ``"worktree", "add", ...``).
    cwd:
        Working directory for the subprocess. ``None`` inherits the current
        process's working directory.

    Raises
    ------
    RuntimeError
        When git exits with a non-zero return code, with stderr included.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
    )
    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {stderr_text}"
        )


class GitWorktreeLifecycle(WorktreeLifecycle):
    """Manage parcel worktrees via ``git worktree add`` / ``git worktree remove``.

    Parameters
    ----------
    base_dir:
        Directory that will hold per-job worktree sub-directories.
        Typically ``./worktrees/`` or ``<repo>/../claude-fleet-worktrees/``.
    branch_prefix:
        Prefix applied to the branch created for each worktree.
        Default ``"parcel/"`` (e.g. ``parcel/job-abc123``).
    base_branch:
        Branch or ref to fork from when creating a new worktree.
        Default ``"origin/main"``. Tests can pass a local branch name.
    repo_root:
        Root of the git repository that owns the worktrees.  If ``None``,
        git is invoked from ``base_dir`` which must be inside the repo.
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        branch_prefix: str = "parcel/",
        base_branch: str = "origin/main",
        repo_root: Path | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.branch_prefix = branch_prefix
        self.base_branch = base_branch
        self.repo_root = Path(repo_root) if repo_root is not None else None

    # ------------------------------------------------------------------
    # WorktreeLifecycle implementation
    # ------------------------------------------------------------------

    async def create(self, job: Job) -> Path:
        """Create a worktree for *job* and return its path.

        Idempotent: returns the existing path when the directory is already
        present (resumes cleanly after a daemon crash).

        The worktree is created at ``base_dir / job.id`` on branch
        ``{branch_prefix}{job.id}`` forked from ``base_branch``.

        Raises
        ------
        RuntimeError
            When ``git worktree add`` fails for any reason other than the
            directory already existing.
        """
        worktree = self.base_dir / job.id
        if worktree.exists():
            return worktree.resolve()

        self.base_dir.mkdir(parents=True, exist_ok=True)
        branch = f"{self.branch_prefix}{job.id}"
        cwd = self.repo_root or self.base_dir
        await _run_git(
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            self.base_branch,
            cwd=cwd,
        )
        return worktree.resolve()

    async def retire(self, job: Job, worktree: Path) -> None:
        """Remove *worktree* and delete the parcel branch.

        Raises
        ------
        ValueError
            When ``job.status != "landed"``.
        RuntimeError
            When ``git worktree remove`` or ``git branch -D`` fails.
        """
        if job.status != "landed":
            raise ValueError(
                f"refusing to retire worktree for non-landed job {job.id!r} "
                f"(status={job.status!r})"
            )
        cwd = self.repo_root or self.base_dir
        await _run_git("worktree", "remove", str(worktree), cwd=cwd)
        branch = f"{self.branch_prefix}{job.id}"
        await _run_git("branch", "-D", branch, cwd=cwd)

    async def exists(self, job: Job) -> Path | None:
        """Return the worktree path if it exists on disk, ``None`` otherwise."""
        worktree = self.base_dir / job.id
        return worktree.resolve() if worktree.exists() else None


__all__ = ["GitWorktreeLifecycle", "WorktreeLifecycle"]
