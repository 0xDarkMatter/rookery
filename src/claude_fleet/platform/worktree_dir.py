"""Resolve and create the parcel worktree directory on disk.

Every ``axiom launch`` subcommand needs the same pair of operations:

* :func:`resolve_worktree_dir` — map a parcel id to its expected on-disk
  location (``<repo>/../Axiom-worktrees/<id>``), **without** creating it.
  Useful for previews / ``--dry-run``.
* :func:`ensure_worktree` — resolve + create the git worktree if absent,
  reusing an existing one otherwise. Equivalent to the ``if [[ -d
  $WORKTREE ]]; then reuse; else git worktree add -b parcel/<id>; fi``
  branch the bash scripts share.

Both helpers stay bench-agnostic and don't spawn claude; they only deal
with filesystem + git plumbing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

WORKTREES_DIRNAME = "Axiom-worktrees"


@dataclass(frozen=True)
class RepoPaths:
    """Resolved paths for a given repo root."""

    repo_root: Path
    worktrees_root: Path
    logs_dir: Path


def repo_paths(repo_root: Path) -> RepoPaths:
    """Return the canonical ``Axiom-worktrees`` layout rooted at *repo_root*.

    The bash scripts build these with ``dirname``/``pwd -W``; here we lean
    on ``Path`` which is cross-platform and doesn't need MSYS path rewrites.
    """
    repo_root = Path(repo_root).resolve()
    worktrees_root = repo_root.parent / WORKTREES_DIRNAME
    return RepoPaths(
        repo_root=repo_root,
        worktrees_root=worktrees_root,
        logs_dir=worktrees_root / "logs",
    )


def resolve_worktree_dir(parcel_id: str, repo_root: Path) -> Path:
    """Return the expected on-disk path for *parcel_id* (not created).

    Example: ``repo_root=/x/Forge/Axiom`` + ``parcel_id=P1`` →
    ``/x/Forge/Axiom-worktrees/P1``.
    """
    return repo_paths(repo_root).worktrees_root / parcel_id


def worktree_exists(parcel_id: str, repo_root: Path) -> bool:
    """True iff ``resolve_worktree_dir(parcel_id)`` is an existing directory."""
    return resolve_worktree_dir(parcel_id, repo_root).is_dir()


def ensure_worktree(
    parcel_id: str,
    repo_root: Path,
    *,
    branch_hint: str | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Ensure a git worktree exists for *parcel_id*; return its path.

    Parameters
    ----------
    parcel_id:
        Slug used as both the worktree directory name and branch suffix.
    repo_root:
        Path to the source repo that owns the worktree.
    branch_hint:
        Branch to create. Defaults to ``parcel/<parcel_id>`` — the
        convention every parcel launch surface uses.
    env:
        Environment for the ``git worktree add`` subprocess. Pass ``None``
        to inherit the caller's env. Callers that run as SYSTEM (e.g. pm2)
        must inject ``GIT_CONFIG_COUNT``/``safe.directory=*`` via this arg
        or git will refuse the cross-user repo with "dubious ownership".

    Returns
    -------
    Path
        Absolute path to the worktree directory (existing or newly created).

    Raises
    ------
    RuntimeError
        When ``git worktree add`` fails. The subprocess stderr is included
        so operators can diagnose common issues (branch already exists,
        dirty working tree, etc.).
    """
    worktree = resolve_worktree_dir(parcel_id, repo_root)
    if worktree.is_dir():
        return worktree

    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch = branch_hint or f"parcel/{parcel_id}"
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "add",
                str(worktree),
                "-b",
                branch,
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git worktree add failed for {parcel_id!r} "
            f"(rc={exc.returncode}): {exc.stderr.strip()}"
        ) from exc
    return worktree
