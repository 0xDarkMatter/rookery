"""Auto-retire landed parcel worktrees.

Symmetric counterpart to :mod:`claude_fleet.orchestrator.land_backend`. Where
landing moves the work onto ``main``, retirement removes the now-redundant
parcel worktree from disk via ``git worktree remove``.

The logic mirrors the manual ``.claude/skills/worktree-retire`` skill's
safety guards, but narrowed to the cases the orchestrator daemon can
positively verify:

1. Job is in the ``landed`` terminal state (daemon's own bookkeeping).
2. Worktree directory still exists on disk.
3. Filesystem has been idle for at least ``idle_minutes`` — no writes
   inside the tree in that window.
4. ``git status --porcelain`` is empty (no uncommitted work, not even a
   ``.pid`` or stray file).
5. The parcel branch is an ancestor of ``main`` (``git merge-base
   --is-ancestor``). ``landed`` implies this by the state machine, but
   we re-verify since landed_commit was recorded at a point-in-time on
   local main.
6. No process has files open inside the worktree. Cross-platform check.

All six green → safe retire. Any red → skip (next tick retries). The
daemon only retires one worktree per tick to bound blast radius.

PARCEL_DONE preservation: before ``git worktree remove`` runs, the
worktree's ``PARCEL_DONE-<job_id>.md`` (or legacy ``PARCEL_DONE.md``) is
copied to ``<project_root>/parcels/done/<job_id>-PARCEL_DONE.md`` so the
audit trail survives the removal.

This module never calls ``rm -rf``, ``git worktree prune``, or
``git branch -D``. Removal is strictly ``git worktree remove <path>``
with no ``--force`` — if git itself refuses (e.g. it still thinks the
working tree is dirty), the gate has made a wrong call and the operator
should investigate rather than the daemon force-retire.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

from claude_fleet.orchestrator.backend import Job

log = structlog.get_logger(__name__)

IS_WINDOWS = sys.platform == "win32"

# Reasons a retirement gate can refuse. Journal payloads use these
# verbatim so operators can grep for specific blockers.
RetireBlockReason = str  # free-form; see RetireCheck.reason


@dataclass(frozen=True)
class RetireCheck:
    """Outcome of :func:`can_auto_retire`."""

    ok: bool
    reason: str | None = None


def _worktree_path(worktrees_root: Path, job_id: str) -> Path:
    """Mirror the layout used by Local/LandBackend: ``<root>/<job_id>``."""
    return worktrees_root / job_id


def _branch(job_id: str) -> str:
    """Mirror LandBackend._branch — ``parcel/<job_id>``."""
    return f"parcel/{job_id}"


def _newest_mtime(path: Path) -> float | None:
    """Newest mtime of any file under *path*. ``None`` if the tree is empty.

    Walks the whole directory. On huge trees this is still fast: the
    daemon only runs it once per tick per landed-not-retired worktree,
    which in practice is <= 10 entries at peak.

    Files the retire gate genuinely doesn't care about (build caches,
    nested ``.git`` metadata) are excluded so a periodic git-gc write
    can't indefinitely reset the idle clock.
    """
    newest: float | None = None
    try:
        for entry in path.rglob("*"):
            # Skip nested git metadata — git's own maintenance touches
            # these without any operator intent.
            if ".git" in entry.parts:
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    except OSError:
        return None
    return newest


def _git_porcelain(worktree: Path) -> str:
    """Return ``git status --porcelain`` output. Empty string = clean."""
    result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # A non-zero status here means we can't verify cleanliness —
        # treat as "dirty" so the gate refuses.
        return result.stderr or "<git status failed>"
    return result.stdout


def _is_ancestor(repo_root: Path, branch: str, main_branch: str) -> bool:
    """Is ``branch`` an ancestor of ``main_branch``?"""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "merge-base",
            "--is-ancestor",
            branch,
            main_branch,
        ],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _has_open_files(worktree: Path) -> bool:
    """Is any process holding a file inside *worktree* open?

    Cross-platform best-effort check. If the probe itself fails (lsof
    missing, handle.exe missing, permission denied), we return ``True``
    — the safer default for a guard whose failure mode is "refuse to
    retire".
    """
    if IS_WINDOWS:
        # ``handle.exe`` from Sysinternals is the canonical probe; it
        # isn't guaranteed to be present. If it isn't, fall back to
        # checking for a live ``.pid`` / ``.winpid`` file at the
        # worktree root — the same signal the manual retire-script uses.
        return _has_open_files_windows(worktree)
    # POSIX (WSL, macOS, Linux) — use lsof if present.
    return _has_open_files_posix(worktree)


def _has_open_files_posix(worktree: Path) -> bool:
    """POSIX probe: ``lsof +D <worktree>`` — any output means open files."""
    lsof = shutil.which("lsof")
    if lsof is None:
        # No probe available. Fall back to the pidfile heuristic.
        return _pidfile_alive(worktree)
    try:
        result = subprocess.run(
            [lsof, "+D", str(worktree)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # probe failure → refuse to retire
    # lsof exits 1 when nothing is open; we key on stdout presence.
    return bool(result.stdout.strip())


def _has_open_files_windows(worktree: Path) -> bool:
    """Windows probe: check ``.pid`` / ``.winpid`` liveness.

    Mirrors the manual ``scripts/worktree-retire.sh`` safety gate. A
    landed worktree shouldn't have live pidfiles anyway — the worker
    finished, the daemon released the slot — but if one is present,
    refuse rather than stomp on a process the daemon has lost track of.
    """
    return _pidfile_alive(worktree)


def _pidfile_alive(worktree: Path) -> bool:
    """Return True if any ``.pid`` / ``.winpid`` file points at a live PID."""
    for name in (".pid", ".winpid"):
        pidfile = worktree / name
        if not pidfile.exists():
            continue
        try:
            pid = int(pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            return True
    return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_windows(pid: int) -> bool:
    """``tasklist //FI "PID eq <pid>"`` — non-empty task list means alive."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # can't tell → assume alive, refuse to retire
    return str(pid) in result.stdout


def _pid_alive_posix(pid: int) -> bool:
    """``os.kill(pid, 0)`` — signal 0 is the canonical liveness probe."""
    import os  # noqa: PLC0415

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # PermissionError etc — process exists but we can't signal it.
        return True
    return True


def can_auto_retire(
    job: Job,
    *,
    worktrees_root: Path,
    repo_root: Path,
    idle_seconds: int,
    main_branch: str = "main",
    now: float | None = None,
) -> RetireCheck:
    """Evaluate all six gates. Returns ``RetireCheck(ok=True)`` iff safe.

    Separate function so tests can drive each gate deterministically and
    the daemon can log the failing reason without re-running the check.
    """

    wt_path = _worktree_path(worktrees_root, job.id)
    current = now if now is not None else time.time()
    reason = _first_failing_gate(
        job,
        wt_path=wt_path,
        repo_root=repo_root,
        idle_seconds=idle_seconds,
        main_branch=main_branch,
        now=current,
    )
    return RetireCheck(ok=reason is None, reason=reason)


def _first_failing_gate(
    job: Job,
    *,
    wt_path: Path,
    repo_root: Path,
    idle_seconds: int,
    main_branch: str,
    now: float,
) -> str | None:
    """Return the name of the first gate that refuses, or ``None`` if all pass.

    Gates are ordered cheap-to-expensive: DB state → filesystem → git
    subprocess → open-files probe. Short-circuits on the first refusal
    so we don't spawn git for jobs that aren't even in ``landed``. The
    closure-based form lazy-evaluates each gate (the ones that spawn
    subprocesses don't run until their turn).
    """
    last_write = _newest_mtime(wt_path) if wt_path.exists() else None
    gates: list[tuple[str, Callable[[], bool]]] = [
        ("job_not_landed", lambda: job.status != "landed"),
        ("worktree_missing", lambda: not wt_path.exists()),
        (
            "recent_write",
            lambda: last_write is not None and (now - last_write) < idle_seconds,
        ),
        ("uncommitted_changes", lambda: bool(_git_porcelain(wt_path).strip())),
        (
            "branch_not_merged",
            lambda: not _is_ancestor(repo_root, _branch(job.id), main_branch),
        ),
        ("active_process", lambda: _has_open_files(wt_path)),
    ]
    for reason, failed in gates:
        if failed():
            return reason
    return None


def _find_parcel_done(worktree: Path, job_id: str) -> Path | None:
    """Return the PARCEL_DONE file for *job_id*, or ``None`` if missing."""
    per_parcel = worktree / f"PARCEL_DONE-{job_id}.md"
    if per_parcel.exists():
        return per_parcel
    legacy = worktree / "PARCEL_DONE.md"
    if legacy.exists():
        return legacy
    return None


@dataclass(frozen=True)
class RetireResult:
    """Outcome of :func:`retire`.

    ``parcel_done_copied`` is the destination path of the archived
    PARCEL_DONE or ``None`` if no PARCEL_DONE was present at the
    worktree root (the gate doesn't require one — jobs can be ``landed``
    without the per-parcel marker when the worker wrote its completion
    signal elsewhere).
    """

    job_id: str
    worktree: Path
    parcel_done_copied: Path | None


def retire(
    job: Job,
    *,
    worktrees_root: Path,
    repo_root: Path,
    project_root: Path,
    idle_seconds: int,
    main_branch: str = "main",
    now: float | None = None,
) -> RetireResult:
    """Retire *job*'s worktree. Preconditions: all six gates green.

    Raises ``RuntimeError`` if the gate re-check refuses, so callers
    that don't pre-check still can't accidentally force a retirement.
    """

    check = can_auto_retire(
        job,
        worktrees_root=worktrees_root,
        repo_root=repo_root,
        idle_seconds=idle_seconds,
        main_branch=main_branch,
        now=now,
    )
    if not check.ok:
        raise RuntimeError(
            f"retire refused for {job.id!r}: {check.reason}"
        )

    wt_path = _worktree_path(worktrees_root, job.id)

    # Preserve PARCEL_DONE before removal. Copy (not move) so a
    # concurrent ``git worktree remove`` race can't leave an empty
    # archive slot — git's own refusal to remove is our second line of
    # defence, and the copy is idempotent if re-run.
    archive_dest: Path | None = None
    pd = _find_parcel_done(wt_path, job.id)
    if pd is not None:
        archive_dir = project_root / "parcels" / "done"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_dest = archive_dir / f"{job.id}-PARCEL_DONE.md"
        shutil.copy(pd, archive_dest)

    # ``git worktree remove`` without --force. Respects git's own
    # safety (refuses on dirty trees, live rebases in progress). If git
    # says no, we respect that — the gate got something wrong.
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "remove", str(wt_path)],
        check=True,
    )

    return RetireResult(
        job_id=job.id,
        worktree=wt_path,
        parcel_done_copied=archive_dest,
    )


__all__ = [
    "RetireCheck",
    "RetireResult",
    "can_auto_retire",
    "retire",
]
