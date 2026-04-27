"""``rookery doctor`` command.

Verifies config + tooling for a rookery project.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from rookery.doctor import CheckResult, run_checks

# ASCII symbols used on narrow/legacy terminals (Windows cmd without Unicode support)
_SYM_OK = "ok"
_SYM_FAIL = "FAIL"
_SYM_SKIP = "skip"

# Unicode symbols used on capable terminals
_SYM_OK_UNI = "✓"   # ✓
_SYM_FAIL_UNI = "✗"  # ✗
_SYM_SKIP_UNI = "─"  # ─

# Detect whether stdout can handle Unicode
def _unicode_ok() -> bool:
    try:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        "✓".encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_USE_UNICODE = _unicode_ok()


def _sym(ok: bool | None) -> str:
    if _USE_UNICODE:
        if ok is True:
            return _SYM_OK_UNI
        if ok is False:
            return _SYM_FAIL_UNI
        return _SYM_SKIP_UNI
    else:
        if ok is True:
            return _SYM_OK
        if ok is False:
            return _SYM_FAIL
        return _SYM_SKIP


def _divider() -> str:
    if _USE_UNICODE:
        return "─" * 49
    return "-" * 49


def _format_line(result: CheckResult) -> str:
    """Build a single display line for *result*."""
    sym = _sym(result.ok)
    name_col = result.name.ljust(26)
    value_part = result.value or ""
    return f"{sym} {name_col}{value_part}"


def doctor_cmd(
    ctx: typer.Context,
    config: str = typer.Option(
        "./rookery.yaml",
        "--config",
        help="Path to rookery.yaml config file.",
        envvar="ROOKERY_CONFIG",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of human output.",
    ),
) -> None:
    """Verify config + tooling for this rookery project.

    Checks: claude binary on PATH, git available, OAuth env,
    DB writable, worktree base dir writable.

    Exits 0 if all checks pass or are skipped. Exits 1 if any check fails.
    """
    config_path = Path(config)
    results = run_checks(config_path=config_path)

    if output_json:
        typer.echo(json.dumps([r.model_dump() for r in results], indent=2))
        any_failed = any(r.ok is False for r in results)
        raise typer.Exit(code=1 if any_failed else 0)

    # Human output
    typer.echo(_divider())
    for result in results:
        typer.echo(_format_line(result))
        if result.ok is False and result.remediation:
            typer.echo(f"  Remediation: {result.remediation}")
    typer.echo(_divider())

    any_failed = any(r.ok is False for r in results)
    if any_failed:
        typer.echo("Some checks failed. See remediation hints above.", err=True)
        raise typer.Exit(code=1)
    else:
        typer.echo("All checks passed.")


__all__ = ["doctor_cmd"]
