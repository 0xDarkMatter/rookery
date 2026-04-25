"""Spawn a detached headless ``claude -p`` session.

The core launch sequence for every parcel worker:

* ``cd`` into the parcel worktree
* Export ``AXIOM_WORKTREE_ID`` so the child uses our canonical id
* ``claude -p [--model ...] --dangerously-skip-permissions`` with the
  prompt piped in via stdin (argv on Windows caps at ~32 KB; our parcel
  prompts regularly exceed that)
* Write ``.pid`` (Python-visible child pid) and ``.winpid`` (Windows
  native pid, identical when spawned from pure Python; both written for
  consumers that read the worktree directly)

Under ``subprocess.Popen`` with ``start_new_session=True`` (POSIX) or
``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` (Windows), the child
survives the parent's exit — the ``nohup + &`` equivalent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.platform.worktree_id import resolve_worktree_id

IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class SpawnResult:
    """Outcome of a headless spawn — useful for ``--json`` output."""

    pid: int
    worktree: Path
    log_path: Path
    prompt_path: Path
    prompt_bytes: int


def prompt_size_bytes(prompt_path: Path) -> int:
    """Return *prompt_path* size in raw bytes via ``stat()``.

    Avoids the ``wc -c`` CRLF bug the bash scripts inherited (on Windows
    Git Bash, ``wc -c`` over-counts by the number of CR bytes versus the
    on-disk size).
    """
    return prompt_path.stat().st_size


def spawn_headless_claude(
    *,
    worktree: Path,
    prompt_path: Path,
    log_path: Path,
    model: str | None = None,
    claude_bin: str = "claude",
    extra_env: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> SpawnResult:
    """Spawn a detached ``claude -p`` session reading *prompt_path* via stdin.

    Parameters
    ----------
    worktree:
        Directory the child runs in (``cwd`` for the subprocess).
    prompt_path:
        Parcel prompt file — piped in via stdin so argv size limits don't apply.
    log_path:
        File to receive the child's stdout+stderr (appended/created).
    model:
        Optional model override. Empty → claude CLI uses its config default.
    claude_bin:
        Executable name or path. Swappable for tests that stand up a no-op
        stub (e.g. a python script emulating the claude CLI).
    extra_env:
        Extra env vars layered onto the base env (``os.environ`` by default,
        or *env* when supplied). Use this when you just want to add a couple
        of keys.
    env:
        Full env override. When supplied, replaces ``os.environ`` as the
        base — use this when the caller has already built the complete
        environment (e.g. orchestrator profile rotation with stripped
        ``CLAUDE_CODE_OAUTH_TOKEN``). ``extra_env`` still layers on top.

    Returns
    -------
    SpawnResult
        Captured pid + resolved paths. The child is NOT waited on — the
        caller owns lifecycle tracking via ``.pid`` / orchestrator.
    """
    if not prompt_path.is_file():
        raise FileNotFoundError(f"prompt not found: {prompt_path}")
    worktree.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    argv: list[str] = [claude_bin, "-p"]
    if model:
        argv.extend(["--model", model])
    argv.append("--dangerously-skip-permissions")

    base_env = dict(env) if env is not None else dict(os.environ)
    # Propagate (or derive) AXIOM_WORKTREE_ID so the child's journal/pigeon
    # emitters see the same canonical id as the parent launch surface.
    base_env.setdefault("AXIOM_WORKTREE_ID", resolve_worktree_id(worktree))
    if extra_env:
        base_env.update(extra_env)
    env = base_env

    popen_kwargs: dict[str, Any] = {
        "cwd": str(worktree),
        "env": env,
    }
    if IS_WINDOWS:
        # DETACHED_PROCESS lets the child outlive its parent console window
        # even when Python itself is killed. Without it, closing the
        # launching shell on Windows propagates CTRL_CLOSE_EVENT to the
        # child and it dies mid-trial.
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:  # pragma: no cover — covered by Linux CI / local dev only
        popen_kwargs["start_new_session"] = True

    # Open files in the launcher so Popen inherits the descriptors. The OS
    # keeps the underlying handles alive for the detached child even after
    # we close our references here.
    stdin_fh = prompt_path.open("rb")
    log_fh = log_path.open("ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv is not user-shell-composed
            argv,
            stdin=stdin_fh,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
    finally:
        stdin_fh.close()
        log_fh.close()

    pid = proc.pid
    (worktree / ".pid").write_text(f"{pid}\n", encoding="utf-8")
    # Windows: subprocess.Popen.pid is already the native Windows PID, so
    # .winpid == .pid here. Kept for consumers that read the worktree
    # directly (admin views, diagnostic scripts).
    (worktree / ".winpid").write_text(f"{pid}\n", encoding="utf-8")

    return SpawnResult(
        pid=pid,
        worktree=worktree,
        log_path=log_path,
        prompt_path=prompt_path,
        prompt_bytes=prompt_size_bytes(prompt_path),
    )
