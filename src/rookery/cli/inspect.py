"""Inspection commands: ``rookery logs`` and ``rookery diff``.

Both resolve a job's on-disk artifacts (worktree dir + log file) from the
queue's config + the job_id.  No subprocess or daemon interaction beyond
the standard SQLite read.

``rookery logs <id> [-f]``  — tail the parcel's stdout log; optional
                                follow + interleaved progress events.
``rookery diff <id>``        — show ``git diff main...HEAD`` inside the
                                worktree (or against any --against ref).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console

from rookery.orchestrator.config import load_config
from rookery.orchestrator.orchestrator import JobNotFound, Orchestrator

console = Console()


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------


def _resolve_worktrees_root(config_path: Path | None) -> Path:
    """Return the worktrees_root from rookery.yaml (or the standard default).

    The default ``./worktrees`` matches what ``rookery init`` writes and what
    the daemon resolves at startup, so logs/diff paths agree with the daemon's
    on-disk layout.
    """
    cfg_path = config_path or Path("./rookery.yaml")
    try:
        cfg = load_config(cfg_path)
    except (OSError, ValueError):
        cfg = None
    if cfg is not None and cfg.worktrees_root is not None:
        return Path(cfg.worktrees_root).resolve()
    return Path("./worktrees").resolve()


def _resolve_paths(
    job_id: str,
    *,
    db_path: Path,
    config_path: Path | None,
) -> tuple[Path, Path]:
    """Return ``(worktree, log_path)`` for *job_id*.

    Validates the job exists in the queue DB; raises typer.Exit(1) with a
    helpful message if not.  Does NOT validate that the worktree or log
    exist on disk — callers handle that case so they can produce
    command-specific error messages (``logs`` says "no log yet"; ``diff``
    says "no worktree").
    """
    orch = Orchestrator(db_path, lease_ttl_s=60)
    try:
        try:
            orch.status(job_id)
        except JobNotFound:
            Console(stderr=True).print(
                f"[red]error:[/red] no such job: {job_id}\n"
                "  Run [cyan]rookery list --all[/cyan] to see available job ids."
            )
            raise typer.Exit(code=1) from None
    finally:
        orch.close()

    worktrees_root = _resolve_worktrees_root(config_path)
    return (
        worktrees_root / job_id,
        worktrees_root / "logs" / f"{job_id}.log",
    )


# ---------------------------------------------------------------------------
# rookery logs <id>
# ---------------------------------------------------------------------------


def _read_last_lines(path: Path, n: int) -> list[str]:
    """Return the last *n* lines of *path*.  Lightweight tail, no full read."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = min(size, max(16_384, n * 256))
            fh.seek(max(0, size - chunk), 0)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


async def _tail_follow(path: Path, *, poll_interval_s: float = 0.5) -> None:
    """Follow *path* until Ctrl-C, printing new lines as they're appended.

    Polling-based (works on Windows + Linux; no inotify dependency).
    Handles file truncation and rotation by re-opening on read errors.
    """
    last_size = path.stat().st_size if path.exists() else 0
    try:
        while True:
            await asyncio.sleep(poll_interval_s)
            if not path.exists():
                continue
            current_size = path.stat().st_size
            if current_size < last_size:
                # File was truncated / rotated; reset
                last_size = 0
            if current_size > last_size:
                with path.open("rb") as fh:
                    fh.seek(last_size)
                    new_bytes = fh.read()
                    last_size = fh.tell()
                sys.stdout.write(new_bytes.decode("utf-8", errors="replace"))
                sys.stdout.flush()
    except (KeyboardInterrupt, asyncio.CancelledError):
        return


def logs_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Parcel id to read logs for."),
    follow: bool = typer.Option(
        False,
        "--follow",
        "-f",
        help="Follow the log file like ``tail -f`` (Ctrl-C to stop).",
    ),
    lines: int = typer.Option(
        100,
        "--lines",
        "-n",
        help="Show the last N lines (default 100).",
    ),
    show_events: bool = typer.Option(
        False,
        "--events",
        help="Also print progress events from the queue DB, oldest first.",
    ),
) -> None:
    """Tail the parcel worker's stdout log.

    Resolves to ``<worktrees_root>/logs/<id>.log`` — the same path the
    WorkerBackend writes to during ``claude -p`` execution.

    Examples::

        rookery logs my-parcel               # last 100 lines
        rookery logs my-parcel -f            # follow live
        rookery logs my-parcel --events      # interleave parcel_events
    """
    db_path = _db_from_ctx(ctx)
    config_path = _config_from_ctx(ctx)
    _, log_path = _resolve_paths(job_id, db_path=db_path, config_path=config_path)

    if not log_path.exists():
        Console(stderr=True).print(
            f"[yellow]no log yet for {job_id}[/yellow] "
            f"(expected at {log_path})\n"
            "  The daemon writes the log when the worker starts. "
            "If the parcel hasn't been claimed yet, there's nothing to read."
        )
        raise typer.Exit(code=0)

    if show_events:
        orch = Orchestrator(db_path, lease_ttl_s=60)
        try:
            events = orch.read_parcel_events(job_id)
        finally:
            orch.close()
        for ev in events:
            console.print(
                f"[dim]{ev['created_at']}[/dim] "
                f"[cyan]{ev['event_type']}[/cyan] "
                f"{ev.get('label') or ''}"
                + (f" — {ev['detail']}" if ev.get("detail") else "")
            )
        if events:
            console.print("[dim]" + ("─" * 60) + "[/dim]")

    for line in _read_last_lines(log_path, lines):
        sys.stdout.write(line + "\n")
    sys.stdout.flush()

    if follow:
        import contextlib  # noqa: PLC0415

        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_tail_follow(log_path))


# ---------------------------------------------------------------------------
# rookery diff <id>
# ---------------------------------------------------------------------------


def _detect_default_branch(worktree: Path) -> str:
    """Best-effort detect of the parent repo's default branch.

    Tries (in order):
    1. ``git symbolic-ref refs/remotes/origin/HEAD``
    2. Looks for a ``main`` branch
    3. Falls back to ``master``
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            ref = proc.stdout.strip()  # e.g. "refs/remotes/origin/main"
            return ref.rsplit("/", 1)[-1]
    except OSError:
        pass

    for candidate in ("main", "master"):
        proc = subprocess.run(
            ["git", "-C", str(worktree), "show-ref", "--verify", f"refs/heads/{candidate}"],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            return candidate
    return "main"


def diff_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Parcel id to diff."),
    against: str = typer.Option(
        "",
        "--against",
        help="Base ref to compare against (default: auto-detected default branch).",
    ),
    stat: bool = typer.Option(
        False,
        "--stat",
        help="Show ``--stat`` summary (file change counts) instead of full diff.",
    ),
    name_only: bool = typer.Option(
        False,
        "--name-only",
        help="Show only changed file names.",
    ),
) -> None:
    """Show ``git diff <base>...HEAD`` inside the parcel worktree.

    The default base is the parent repo's default branch (auto-detected via
    ``git symbolic-ref origin/HEAD``, falling back to ``main`` then
    ``master``).  Pipes through ``delta`` if it's on PATH for prettier
    output; falls back to plain ``git diff --color`` otherwise.

    Examples::

        rookery diff my-parcel                    # full diff vs main
        rookery diff my-parcel --stat             # file change summary
        rookery diff my-parcel --against develop  # compare against a different ref
    """
    db_path = _db_from_ctx(ctx)
    config_path = _config_from_ctx(ctx)
    worktree, _ = _resolve_paths(job_id, db_path=db_path, config_path=config_path)

    if not worktree.is_dir() or not (worktree / ".git").exists():
        Console(stderr=True).print(
            f"[yellow]no worktree for {job_id}[/yellow] (expected at {worktree})\n"
            "  Either the parcel hasn't been claimed yet, the worktree was "
            "retired, or the path differs from the configured worktrees_root."
        )
        raise typer.Exit(code=0)

    base = against or _detect_default_branch(worktree)

    git_cmd = ["git", "-C", str(worktree), "diff", f"{base}...HEAD"]
    if stat:
        git_cmd.append("--stat")
    elif name_only:
        git_cmd.append("--name-only")
    else:
        # Force colour even when piped through delta or captured by tests;
        # delta strips ANSI it doesn't want anyway.
        git_cmd.append("--color=always")

    use_delta = (
        not stat
        and not name_only
        and shutil.which("delta") is not None
    )

    if use_delta:
        # Two-stage pipe: git → delta.  Capture delta's stdout so it routes
        # through our Console (and is testable via CliRunner).
        plain_cmd = ["git", "-C", str(worktree), "diff", f"{base}...HEAD"]
        with subprocess.Popen(plain_cmd, stdout=subprocess.PIPE) as git_proc:
            delta = subprocess.run(
                ["delta"],
                stdin=git_proc.stdout,
                capture_output=True,
                text=True,
                check=False,
            )
            if git_proc.stdout is not None:
                git_proc.stdout.close()
        if delta.stdout:
            sys.stdout.write(delta.stdout)
            sys.stdout.flush()
    else:
        # Capture so output routes through stdout (and is visible to
        # ``CliRunner.invoke`` in tests).  ``--color=always`` is set above
        # for the non-stat / non-name-only path.
        proc = subprocess.run(git_cmd, capture_output=True, text=True, check=False)
        if proc.stdout:
            sys.stdout.write(proc.stdout)
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# Wiring helpers — re-import from cli.queue for ctx access
# ---------------------------------------------------------------------------


def _db_from_ctx(ctx: typer.Context) -> Path:
    if ctx.obj and "db" in ctx.obj:
        return Path(ctx.obj["db"])
    return Path("./rookery.db")


def _config_from_ctx(ctx: typer.Context) -> Path | None:
    if ctx.obj and "config" in ctx.obj:
        return Path(ctx.obj["config"])
    return None


__all__ = ["logs_cmd", "diff_cmd"]
