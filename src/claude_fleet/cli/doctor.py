"""``claude-fleet doctor`` command.

Verifies config + tooling for a claude-fleet project.

TODO(P6 G7): implement full doctor checks.
"""

from __future__ import annotations

import typer


def doctor_cmd(ctx: typer.Context) -> None:
    """Verify config + tooling for this claude-fleet project.

    Checks: claude binary on PATH, git available, OAuth env,
    DB writable, worktree base dir writable.

    TODO(P6 G7): implement all checks.
    """
    # TODO(P6 G7): implement — run all doctor checks, report green/red
    typer.echo(
        "TODO: doctor is not yet implemented. Implement in P6 (G7).",
        err=True,
    )
    raise typer.Exit(code=1)


__all__ = ["doctor_cmd"]
