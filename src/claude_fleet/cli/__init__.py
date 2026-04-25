"""Top-level ``claude-fleet`` Typer application.

Mounts all sub-apps and registers top-level commands. The root callback
provides global ``--config`` / ``--db`` options that sub-commands read via
``typer.Context``.
"""

from __future__ import annotations

import typer

from claude_fleet.cli.daemon import daemon_app
from claude_fleet.cli.init import init_cmd
from claude_fleet.cli.doctor import doctor_cmd
from claude_fleet.cli.land import land_app, land_cmd, land_history_cmd
from claude_fleet.cli.parcel import parcel_app
from claude_fleet.cli.queue import (
    cancel_cmd,
    enqueue_cmd,
    list_cmd,
    reclaim_cmd,
    requeue_cmd,
    status_cmd,
    summary_cmd,
)
from claude_fleet.cli.worktree import worktree_app

app = typer.Typer(
    name="claude-fleet",
    help="Persistent parcel-dispatch queue + async daemon for parallel claude -p sessions.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def _root_callback(
    ctx: typer.Context,
    config: str = typer.Option(
        "./claude-fleet.yaml",
        "--config",
        help="Path to claude-fleet.yaml config file.",
        envvar="CLAUDE_FLEET_CONFIG",
    ),
    db: str = typer.Option(
        "./claude-fleet.db",
        "--db",
        help="Path to SQLite database file.",
        envvar="CLAUDE_FLEET_DB",
    ),
) -> None:
    """claude-fleet: orchestrate unattended parallel claude -p sessions."""
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

# Top-level land commands (also accessible as `claude-fleet land <id>`)
app.command("land-history")(land_history_cmd)

# Setup / diagnostic commands
app.command("init")(init_cmd)
app.command("doctor")(doctor_cmd)

# Convenience daemon commands at top level
from claude_fleet.cli.daemon import daemon_stop_cmd, daemon_status_cmd  # noqa: E402

app.command("daemon-stop")(daemon_stop_cmd)
app.command("daemon-status")(daemon_status_cmd)

__all__ = ["app"]
