"""CLI entry point for the ``claude-fleet`` console script.

The ``cli_main`` function is referenced directly in ``pyproject.toml``
``[project.scripts]``:

    claude-fleet = "claude_fleet.cli.main:cli_main"
"""

from __future__ import annotations

import sys


def cli_main() -> None:
    """Invoke the root Typer app. Called by the ``claude-fleet`` console script."""
    from claude_fleet.cli import app  # noqa: PLC0415 — lazy import keeps startup fast

    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli_main()
