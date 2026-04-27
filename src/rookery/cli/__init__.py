"""Top-level ``rookery`` Typer application.

Mounts all sub-apps and registers top-level commands. The root callback
provides global ``--config`` / ``--db`` options that sub-commands read via
``typer.Context``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import typer

from rookery.cli.daemon import daemon_app
from rookery.cli.doctor import doctor_cmd
from rookery.cli.init import init_cmd
from rookery.cli.land import land_app, land_history_cmd
from rookery.cli.parcel import parcel_app
from rookery.cli.queue import (
    cancel_cmd,
    enqueue_cmd,
    list_cmd,
    reclaim_cmd,
    requeue_cmd,
    status_cmd,
    summary_cmd,
)
from rookery.cli.worktree import worktree_app

app = typer.Typer(
    name="rookery",
    help="Persistent parcel-dispatch queue + async daemon for parallel claude -p sessions.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        v = _pkg_version("rookery")
    except PackageNotFoundError:
        v = "unknown (not installed)"
    typer.echo(f"rookery {v}")
    raise typer.Exit()


@app.callback(invoke_without_command=True)
def _root_callback(
    ctx: typer.Context,
    config: str = typer.Option(
        "./rookery.yaml",
        "--config",
        help="Path to rookery.yaml config file.",
        envvar="ROOKERY_CONFIG",
    ),
    db: str = typer.Option(
        "./rookery.db",
        "--db",
        help="Path to SQLite database file.",
        envvar="ROOKERY_DB",
    ),
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print the rookery version and exit.",
    ),
) -> None:
    """rookery: orchestrate unattended parallel claude -p sessions."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["db"] = db


# Mount sub-apps
app.add_typer(parcel_app, name="parcel")
app.add_typer(worktree_app, name="worktree")
app.add_typer(daemon_app, name="daemon")

# Mount land as both a sub-app (for sub-commands) and top-level commands
app.add_typer(land_app, name="land")

# Top-level queue commands
app.command("enqueue")(enqueue_cmd)
app.command("list")(list_cmd)
app.command("status")(status_cmd)
app.command("requeue")(requeue_cmd)
app.command("cancel")(cancel_cmd)
app.command("summary")(summary_cmd)
app.command("reclaim")(reclaim_cmd)

# Top-level land commands (also accessible as `rookery land <id>`)
app.command("land-history")(land_history_cmd)

# Setup / diagnostic commands
app.command("init")(init_cmd)
app.command("doctor")(doctor_cmd)

# Convenience daemon commands at top level
from rookery.cli.daemon import daemon_status_cmd, daemon_stop_cmd  # noqa: E402

app.command("daemon-stop")(daemon_stop_cmd)
app.command("daemon-status")(daemon_status_cmd)

__all__ = ["app"]
