"""Resolve a stable per-worktree identifier.

Resolution order (first non-empty wins):

1. Explicit env var: ``$CLAUDE_FLEET_WORKTREE_ID`` (caller-set override; also
   used to propagate the id into child sessions).
2. Git derivation: hash of (``git-common-dir`` + ``worktree-path``), truncated.
   Stable as long as the worktree directory is not renamed. Branch rename does
   NOT drift the id (branch is intentionally absent from the hash input).
3. Fallback: hash of (``hostname`` + normalised cwd), truncated. Used only when
   git derivation fails (rare; mostly bench / test paths).

Outputs are lowercase hex, 8 characters — roughly 2^32 values, fine for a
50-worktree deployment (birthday-bound ~65k). Paths are normalised to forward
slashes and lowercased before hashing (Windows drive-letter casing differs
between sessions).

Consumers read the canonical id via the env var:

    import os
    worktree_id = os.environ.get("CLAUDE_FLEET_WORKTREE_ID") or resolve_worktree_id()

``resolve_worktree_id`` is idempotent and never raises — the fallback path
always produces a usable id.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
from pathlib import Path

from pydantic import BaseModel

_ENV_VAR = "CLAUDE_FLEET_WORKTREE_ID"
_ID_LENGTH = 8
_ENV_MAX_LENGTH = 12
_GIT_TIMEOUT_S = 2.0


def _hash(text: str, length: int = _ID_LENGTH) -> str:
    """Short lowercase-hex SHA256 prefix of the input text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _normalise_path(path: Path | str) -> str:
    """Lowercase + forward-slash for cross-platform stability."""
    s = str(path).replace("\\", "/").rstrip("/")
    return s.lower()


def _run_git(cwd: Path, *args: str) -> str | None:
    """Run a short git rev-parse command; return stripped stdout or None on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    out = proc.stdout.strip()
    return out or None


def derive_worktree_id_from_git(cwd: Path | None = None) -> str | None:
    """Hash ``git-common-dir`` + worktree top-level; return None when not a git worktree.

    The hash inputs are intentionally path-based — NOT branch name. Branch
    rename is a routine operation and must not drift the id.
    """
    cwd = cwd or Path.cwd()
    common_dir = _run_git(cwd, "rev-parse", "--git-common-dir")
    top = _run_git(cwd, "rev-parse", "--show-toplevel")
    if not common_dir or not top:
        return None
    key = f"{_normalise_path(common_dir)}|{_normalise_path(top)}"
    return _hash(key)


def derive_worktree_id_fallback(cwd: Path | None = None) -> str:
    """Hash hostname + cwd. Stable across sessions on the same host."""
    cwd = cwd or Path.cwd()
    key = f"{socket.gethostname().lower()}|{_normalise_path(cwd)}"
    return _hash(key)


def resolve_worktree_id(cwd: Path | None = None) -> str:
    """Resolve the per-worktree identifier. Never raises.

    Resolution order: env override → git derivation → hostname+cwd fallback.
    """
    env = os.environ.get(_ENV_VAR, "").strip()
    if env:
        return env.lower()[:_ENV_MAX_LENGTH]
    git_id = derive_worktree_id_from_git(cwd)
    if git_id:
        return git_id
    return derive_worktree_id_fallback(cwd)


class WorktreeIdentity(BaseModel):
    """Rich identity: id + provenance, for display and diagnostics."""

    worktree_id: str
    source: str
    cwd: str
    git_common_dir: str | None = None
    git_top_level: str | None = None
    git_branch: str | None = None


def resolve_identity(cwd: Path | None = None) -> WorktreeIdentity:
    """Resolve id + provenance for display. Never raises.

    ``source`` is one of ``"env"``, ``"git"``, ``"fallback"``.
    """
    cwd = cwd or Path.cwd()
    cwd_str = _normalise_path(cwd)

    env = os.environ.get(_ENV_VAR, "").strip()
    if env:
        return WorktreeIdentity(
            worktree_id=env.lower()[:_ENV_MAX_LENGTH],
            source="env",
            cwd=cwd_str,
            git_common_dir=_run_git(cwd, "rev-parse", "--git-common-dir"),
            git_top_level=_run_git(cwd, "rev-parse", "--show-toplevel"),
            git_branch=_run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
        )

    common_dir = _run_git(cwd, "rev-parse", "--git-common-dir")
    top = _run_git(cwd, "rev-parse", "--show-toplevel")
    branch = _run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if common_dir and top:
        key = f"{_normalise_path(common_dir)}|{_normalise_path(top)}"
        return WorktreeIdentity(
            worktree_id=_hash(key),
            source="git",
            cwd=cwd_str,
            git_common_dir=common_dir,
            git_top_level=top,
            git_branch=branch,
        )

    return WorktreeIdentity(
        worktree_id=derive_worktree_id_fallback(cwd),
        source="fallback",
        cwd=cwd_str,
        git_common_dir=common_dir,
        git_top_level=top,
        git_branch=branch,
    )
