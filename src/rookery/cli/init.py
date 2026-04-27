"""``rookery init`` command.

Scaffolds a new rookery project in the current directory.
"""

from __future__ import annotations

from pathlib import Path

import typer

from rookery.init import InitError, cmd_init


def init_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing rookery.yaml and re-run migrations.",
    ),
) -> None:
    """Scaffold a rookery project in the current directory.

    Creates: rookery.yaml, rookery.db (with schema migrations),
    parcels/, worktrees/.gitignore, and .gitignore entries.
    """
    try:
        cmd_init(target_dir=Path("."), force=force)
    except InitError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        "Created rookery.yaml, rookery.db, parcels/, worktrees/.gitignore"
    )


__all__ = ["init_cmd"]
