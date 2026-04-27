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
* ``retire()`` raises :exc:`WorktreeRetireError` when the worktree has
  uncommitted changes, so dirty-state is surfaced before any removal.
* ``retire()`` retries ``git worktree remove`` up to 3 times with 1 s
  back-off on Windows to survive transient file-lock races.
* ``exists()`` is a lightweight stat check, not a git-porcelain call.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rookery.orchestrator.backend import Job


@dataclass
class OrphanInfo:
    """Information about a worktree directory that has no live job."""

    path: Path
    reason: str
    last_modified: datetime


# Terminal job statuses: a worktree in one of these is a candidate for sweep
# once it has aged past ``orphan_age_hours``.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"failed", "blocked", "landed"})


async def find_orphans(
    worktree_base: Path,
    db_path: Path,
    orphan_age_hours: float = 168.0,
) -> list[OrphanInfo]:
    """Return worktree directories under *worktree_base* that are orphaned.

    A directory is considered an orphan when:

    * No row exists in the ``jobs`` table whose ``id`` matches the directory
      name, **or**
    * The matching ``jobs`` row has status in ``{failed, blocked, landed}``
      (terminal) **and** the directory's ``mtime`` is older than
      *orphan_age_hours*.

    Parameters
    ----------
    worktree_base:
        Root directory that holds per-job worktree sub-directories.
    db_path:
        Path to the SQLite database containing the ``jobs`` table.
    orphan_age_hours:
        Age threshold (in hours) applied to terminal-status worktrees.
        Defaults to 168 h (7 days).

    Returns
    -------
    list[OrphanInfo]
        Sorted by *path* for deterministic output.
    """
    if not worktree_base.is_dir():
        return []

    now_utc = datetime.now(tz=timezone.utc)

    # Open a read-only connection — sweep should never mutate the DB.
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        orphans: list[OrphanInfo] = []
        for entry in sorted(worktree_base.iterdir()):
            if not entry.is_dir():
                continue
            job_id = entry.name

            # mtime of the worktree directory itself.
            mtime_ts = entry.stat().st_mtime
            last_modified = datetime.fromtimestamp(mtime_ts, tz=timezone.utc)
            age_hours = (now_utc - last_modified).total_seconds() / 3600.0

            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()

            if row is None:
                # No matching job at all — always orphaned.
                orphans.append(
                    OrphanInfo(
                        path=entry.resolve(),
                        reason="no jobs row",
                        last_modified=last_modified,
                    )
                )
            elif row["status"] in _TERMINAL_STATUSES and age_hours >= orphan_age_hours:
                orphans.append(
                    OrphanInfo(
                        path=entry.resolve(),
                        reason=f"terminal status={row['status']!r}, age={age_hours:.1f}h",
                        last_modified=last_modified,
                    )
                )
            # Otherwise: active/non-terminal, or terminal but too recent → keep.
    finally:
        conn.close()

    return orphans


class WorktreeRetireError(RuntimeError):
    """Raised when ``retire()`` refuses due to a safety gate.

    Separate from plain :exc:`RuntimeError` so callers can distinguish
    a deliberate refusal (dirty state, non-landed status) from an
    unexpected git failure.
    """


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


async def _git_status_porcelain(worktree: Path) -> str:
    """Return ``git status --porcelain`` output for *worktree*.

    Empty string means clean. A non-zero exit code is treated as "dirty"
    (can't verify cleanliness) so the retire gate refuses rather than
    silently proceeding past an unverifiable state.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(worktree),
            "status",
            "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            # Non-zero exit means git couldn't run status — treat as dirty.
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            return stderr_text or "<git status failed>"
        return stdout_bytes.decode("utf-8", errors="replace")
    except OSError as exc:
        # git not on PATH or other OS error — treat as dirty.
        return f"<git status error: {exc}>"


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
        Typically ``./worktrees/`` or ``<repo>/../rookery-worktrees/``.
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

        Safety gates (in order):
        1. ``job.status`` must be ``"landed"`` — raises :exc:`ValueError`
           otherwise so callers can't accidentally retire in-flight jobs.
        2. Worktree must be clean (``git status --porcelain`` empty) —
           raises :exc:`WorktreeRetireError` if uncommitted changes are
           present.
        3. ``git worktree remove`` is retried up to 3 times with 1 s
           back-off on Windows to survive transient file-lock races.
           After exhausting retries the underlying :exc:`RuntimeError` is
           re-raised.

        Raises
        ------
        ValueError
            When ``job.status != "landed"``.
        WorktreeRetireError
            When the worktree has uncommitted changes.
        RuntimeError
            When ``git worktree remove`` or ``git branch -D`` fails after
            all retries.
        """
        if job.status != "landed":
            raise ValueError(
                f"refusing to retire worktree for non-landed job {job.id!r} "
                f"(status={job.status!r})"
            )

        # Gate 2: refuse on dirty working tree before touching anything.
        porcelain_out = await _git_status_porcelain(worktree)
        if porcelain_out.strip():
            raise WorktreeRetireError(
                f"uncommitted changes in worktree {worktree!r} for job "
                f"{job.id!r} — refusing to retire dirty worktree"
            )

        cwd = self.repo_root or self.base_dir

        # Gate 3: retry ``git worktree remove`` to handle Windows file locks.
        _RETRY_ATTEMPTS = 3
        _RETRY_BACKOFF_S = 1.0
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                await _run_git("worktree", "remove", str(worktree), cwd=cwd)
                last_exc = None
                break
            except RuntimeError as exc:
                last_exc = exc
                if attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BACKOFF_S)
        if last_exc is not None:
            raise last_exc

        branch = f"{self.branch_prefix}{job.id}"
        await _run_git("branch", "-D", branch, cwd=cwd)

    async def exists(self, job: Job) -> Path | None:
        """Return the worktree path if it exists on disk, ``None`` otherwise."""
        worktree = self.base_dir / job.id
        return worktree.resolve() if worktree.exists() else None

    async def force_remove(self, worktree: Path) -> None:
        """Forcibly remove *worktree* regardless of job status or dirty state.

        Unlike :meth:`retire` (which requires ``status == "landed"`` and a
        clean working tree), ``force_remove`` is intended for orphan-cleanup
        paths where no live job exists for the worktree.  It runs:

        1. ``git worktree remove --force <worktree>``
        2. ``git branch -D <branch>`` for the branch derived from the
           worktree directory name (best-effort; logged on failure).

        Parameters
        ----------
        worktree:
            Absolute or relative path to the worktree directory.

        Raises
        ------
        RuntimeError
            When ``git worktree remove --force`` fails.
        """
        cwd = self.repo_root or self.base_dir
        await _run_git("worktree", "remove", "--force", str(worktree), cwd=cwd)

        # Best-effort branch deletion: derive branch name from worktree dir name.
        branch = f"{self.branch_prefix}{worktree.name}"
        try:
            await _run_git("branch", "-D", branch, cwd=cwd)
        except RuntimeError:
            # Branch may already be gone or may never have existed — not fatal.
            pass


__all__ = [
    "GitWorktreeLifecycle",
    "OrphanInfo",
    "WorktreeLifecycle",
    "WorktreeRetireError",
    "find_orphans",
]
