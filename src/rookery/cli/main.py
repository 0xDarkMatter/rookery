"""CLI entry point for the ``rookery`` console script.

The ``cli_main`` function is referenced directly in ``pyproject.toml``
``[project.scripts]``:

    rookery = "rookery.cli.main:cli_main"
"""

from __future__ import annotations

import sys


def cli_main() -> None:
    """Invoke the root Typer app. Called by the ``rookery`` console script."""
    from rookery.cli import app  # noqa: PLC0415 — lazy import keeps startup fast

    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli_main()
