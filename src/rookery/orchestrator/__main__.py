"""Entrypoint for the ``rookery-daemon`` console script.

Runs the async daemon loop in the foreground until SIGINT/SIGTERM.

Canonical invocation::

    rookery-daemon                       # uses ./rookery.yaml + ./rookery.db
    rookery-daemon --config <path>       # override config path
    rookery-daemon --profiles a,b,c      # round-robin OAuth profiles

(Typer collapses the single ``@app.command`` into a flat-options app, so
no ``start`` subcommand is exposed even though one is decorated below.
The semantics are the same as ``rookery-daemon start ...`` would have been.)

This module is also the ``python -m rookery.orchestrator`` entry point.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import typer

app = typer.Typer(
    name="rookery-daemon",
    help="rookery daemon — run the async orchestrator in the foreground.",
    no_args_is_help=True,
    add_completion=False,
)

_DEFAULT_PIDFILE = Path("rookery.pid")


@app.command("start")
def start_cmd(
    config: str = typer.Option(
        "./rookery.yaml",
        "--config",
        help="Path to rookery.yaml config file.",
        envvar="ROOKERY_CONFIG",
    ),
    db: str = typer.Option(
        "",
        "--db",
        help="Override db_path from config (defaults to config value or ./rookery.db).",
        envvar="ROOKERY_DB",
    ),
    pidfile: str = typer.Option(
        "",
        "--pidfile",
        help=f"Where to write the running pid (default {_DEFAULT_PIDFILE}).",
    ),
    profiles: str = typer.Option(
        "",
        "--profiles",
        help=(
            "Comma-separated claude profile ring for round-robin spawning. "
            "Sets ROOKERY_PROFILES env var. "
            "Empty = single-account mode."
        ),
    ),
    strip_auth_env: bool = typer.Option(
        False,
        "--strip-auth-env",
        help=(
            "Unset CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY before daemon "
            "start so workers use profile .credentials.json."
        ),
    ),
) -> None:
    """Run the async daemon loop in the foreground until SIGINT/SIGTERM.

    Canonical invocation for pm2 / systemd / docker deployments.
    The daemon terminates child workers on shutdown and flips their jobs
    back to pending so the next start picks them up.
    """

    # Force UTF-8 stdio on Windows to prevent encoding crashes in structlog
    for stream in (sys.stdout, sys.stderr):
        with suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    # Strip stale auth env if requested
    if strip_auth_env:
        from rich.console import Console  # noqa: PLC0415

        _console = Console()
        stripped = [
            v
            for v in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
            if os.environ.pop(v, None) is not None
        ]
        if stripped:
            _console.print(f"[yellow]stripped auth env:[/yellow] {', '.join(stripped)}")

    if profiles:
        os.environ["ROOKERY_PROFILES"] = profiles

    from rich.console import Console  # noqa: PLC0415

    from rookery.orchestrator.config import load_config  # noqa: PLC0415
    from rookery.orchestrator.daemon import run_daemon  # noqa: PLC0415
    from rookery.orchestrator.land_backend import LandBackend  # noqa: PLC0415
    from rookery.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415
    from rookery.orchestrator.worker_backend import WorkerBackend  # noqa: PLC0415
    from rookery.worktree import GitWorktreeLifecycle  # noqa: PLC0415

    console = Console()

    # Resolution order:
    #   1. explicit --db flag (or ROOKERY_DB env var, wired via typer envvar=)
    #   2. yaml db_path / worktrees_root from --config file (load_config handles
    #      the deprecated 'worktree_base' alias automatically)
    #   3. hardcoded fallback ./rookery.db when neither source sets db_path
    cfg = load_config(Path(config))

    # Apply the hardcoded db_path fallback only when the yaml also didn't set it
    # (load_config returns the pydantic default .data/orchestrator.db in that case)
    if cfg.db_path == Path(".data/orchestrator.db"):
        cfg = cfg.model_copy(update={"db_path": Path("./rookery.db")})

    # --db flag (or ROOKERY_DB) wins over everything
    if db:
        cfg = cfg.model_copy(update={"db_path": Path(db)})

    resolved_pidfile = Path(pidfile) if pidfile else _DEFAULT_PIDFILE
    repo_root = Path.cwd()
    worktrees_root = cfg.worktrees_root or repo_root / "worktrees"

    orch = Orchestrator(cfg.db_path, lease_ttl_s=cfg.lease_ttl_s)
    worktree_lifecycle = GitWorktreeLifecycle(
        base_dir=worktrees_root,
        repo_root=repo_root,
    )
    backend = WorkerBackend(
        repo_root=repo_root,
        worktrees_root=worktrees_root,
        shutdown_grace_s=cfg.shutdown_grace_s,
        claude_profile=cfg.claude_profile,
        worktree_lifecycle=worktree_lifecycle,
        # v0.3: enable ROOKERY_DB env var injection so workers can invoke
        # ``rookery parcel done`` to report verdicts directly to the queue DB.
        db_path=cfg.db_path,
    )

    land_backend: LandBackend | None = None
    if cfg.auto_land:
        land_backend = LandBackend(
            repo_root=repo_root,
            worktrees_root=worktrees_root,
            test_cmd=cfg.auto_land_test_cmd,
            timeout_s=cfg.auto_land_timeout_s,
        )

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        console.print("[yellow]shutdown requested[/yellow]")
        stop_event.set()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, _on_signal)
        try:
            await run_daemon(
                orch,
                backend,
                stop_event,
                tick_interval_s=cfg.tick_interval_s,
                max_concurrent=cfg.max_concurrent,
                auto_land=cfg.auto_land,
                land_backend=land_backend,
                auto_retire=cfg.auto_retire,
                retire_worktrees_root=worktrees_root,
                retire_repo_root=repo_root,
                retire_project_root=repo_root,
                retire_idle_minutes=cfg.auto_retire_idle_minutes,
                retire_batch_size=cfg.auto_retire_batch_size,
            )
        finally:
            orch.close()

    resolved_pidfile.parent.mkdir(parents=True, exist_ok=True)
    resolved_pidfile.write_text(str(os.getpid()), encoding="utf-8")
    console.print(
        f"[green]daemon starting[/green] db={cfg.db_path} "
        f"max_concurrent={cfg.max_concurrent} auto_land={cfg.auto_land} "
        f"pidfile={resolved_pidfile}"
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        with suppress(OSError):
            resolved_pidfile.unlink()
    console.print("[dim]daemon stopped[/dim]")


def main() -> None:
    """Entry point for the ``rookery-daemon`` console script."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
