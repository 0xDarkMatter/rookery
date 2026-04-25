"""``claude-fleet init`` command.

Scaffolds a new claude-fleet project in the current directory.

TODO(P5 G6): implement full init logic.
"""

from __future__ import annotations

import typer


def init_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing files.",
    ),
) -> None:
    """Scaffold a claude-fleet project in the current directory.

    Creates: claude-fleet.yaml, claude-fleet.db (empty schema),
    parcels/, worktrees/.gitignore.

    TODO(P5 G6): implement full scaffolding logic.
    """
    # TODO(P5 G6): implement — write claude-fleet.yaml, init DB, create dirs
    typer.echo(
        "TODO: init is not yet implemented. Implement in P5 (G6).",
        err=True,
    )
    raise typer.Exit(code=1)


__all__ = ["init_cmd"]
