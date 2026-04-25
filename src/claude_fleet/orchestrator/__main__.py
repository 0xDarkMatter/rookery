"""Entrypoint: ``python -m axiom.orchestrator`` defers to the Typer CLI."""

from __future__ import annotations

from axiom.orchestrator.cli import app

if __name__ == "__main__":
    app()
