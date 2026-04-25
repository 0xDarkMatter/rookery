"""Entrypoint: ``python -m claude_fleet.orchestrator`` defers to the Typer CLI.

TODO(P3): wire up claude_fleet.cli once the CLI surface is built.
"""

from __future__ import annotations

if __name__ == "__main__":
    # TODO(P3): replace with `from claude_fleet.cli.main import cli_main; cli_main()`
    raise NotImplementedError(
        "CLI not yet implemented. Run `python -m claude_fleet.orchestrator` "
        "after P3 (CLI surface) is complete."
    )
