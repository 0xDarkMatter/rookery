"""``claude-fleet init`` command.

Scaffolds a new claude-fleet project in the current directory.
"""

from __future__ import annotations

from pathlib import Path

import typer

from claude_fleet.init import InitError, cmd_init


def init_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing claude-fleet.yaml and re-run migrations.",
    ),
) -> None:
    """Scaffold a claude-fleet project in the current directory.

    Creates: claude-fleet.yaml, claude-fleet.db (with schema migrations),
    parcels/, worktrees/.gitignore, and .gitignore entries.
    """
    try:
        cmd_init(target_dir=Path("."), force=force)
    except InitError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        "Created claude-fleet.yaml, claude-fleet.db, parcels/, worktrees/.gitignore"
    )


__all__ = ["init_cmd"]
