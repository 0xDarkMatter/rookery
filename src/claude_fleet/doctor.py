"""Preflight checks for ``claude-fleet doctor``.

Each check returns a :class:`CheckResult`.  The full suite runs via
:func:`run_checks` which collects *all* results without stopping on the
first failure.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from claude_fleet.orchestrator.config import OrchestratorConfig
from claude_fleet.orchestrator.schema import MIGRATIONS_DIR


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class CheckResult(BaseModel):
    """Result of a single preflight check.

    Attributes:
        name: Human-readable check name.
        ok: True = passed, False = failed, None = skipped.
        value: Short string describing what was found (e.g. a path or version).
        remediation: Suggested fix shown when *ok* is False.
    """

    name: str
    ok: bool | None  # None → skipped
    value: str | None = None
    remediation: str | None = None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_claude_binary(config: OrchestratorConfig | None) -> CheckResult:
    """Check 1: claude binary on PATH (or config-overridden path)."""
    # Config may specify a custom binary name/path via an attribute that
    # doesn't yet exist in the model — fall back gracefully.
    binary: str = "claude"
    if config is not None:
        binary = getattr(config, "claude_binary", "claude") or "claude"

    found = shutil.which(binary)
    if found:
        return CheckResult(name="claude binary", ok=True, value=found)
    return CheckResult(
        name="claude binary",
        ok=False,
        value=None,
        remediation=(
            f"'{binary}' not found on PATH. "
            "Install Claude Code CLI: https://claude.ai/code"
        ),
    )


def _check_git() -> CheckResult:
    """Check 2: git available, version >= 2.5 (worktree support)."""
    git = shutil.which("git")
    if not git:
        return CheckResult(
            name="git",
            ok=False,
            value=None,
            remediation="git not found on PATH. Install git >= 2.5.",
        )

    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        raw = result.stdout.strip()  # e.g. "git version 2.42.0"
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="git",
            ok=False,
            value=None,
            remediation=f"Failed to run 'git --version': {exc}",
        )

    # Parse version string — take last token that looks like N.N…
    version_str: str | None = None
    for part in reversed(raw.split()):
        if part[0].isdigit():
            version_str = part
            break

    if version_str is None:
        return CheckResult(
            name="git",
            ok=False,
            value=raw or None,
            remediation="Could not parse git version. Ensure git >= 2.5 is installed.",
        )

    # Compare major.minor
    parts = version_str.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return CheckResult(
            name="git",
            ok=False,
            value=version_str,
            remediation="Could not parse git version. Ensure git >= 2.5 is installed.",
        )

    if (major, minor) >= (2, 5):
        return CheckResult(name="git", ok=True, value=version_str)
    return CheckResult(
        name="git",
        ok=False,
        value=version_str,
        remediation=f"git {version_str} is too old. Worktree support requires >= 2.5.",
    )


def _check_config_file(config_path: Path) -> tuple[CheckResult, OrchestratorConfig | None]:
    """Check 3: config file readable + parses as valid OrchestratorConfig.

    Returns both the CheckResult and the parsed config (or None on failure) so
    subsequent checks can use the config values.
    """
    if not config_path.exists():
        return (
            CheckResult(
                name="config file",
                ok=False,
                value=str(config_path),
                remediation=(
                    f"Config file '{config_path}' not found. "
                    "Run 'claude-fleet init' to create one."
                ),
            ),
            None,
        )

    try:
        raw = config_path.read_text(encoding="utf-8")
        data: Any = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001
        return (
            CheckResult(
                name="config file",
                ok=False,
                value=str(config_path),
                remediation=f"Failed to read config: {exc}",
            ),
            None,
        )

    if not isinstance(data, dict):
        return (
            CheckResult(
                name="config file",
                ok=False,
                value=str(config_path),
                remediation="Config file must be a YAML mapping at the top level.",
            ),
            None,
        )

    try:
        # Only validate fields the model knows about (config may have extra
        # comments or future fields the current model doesn't declare)
        known = set(OrchestratorConfig.model_fields)
        filtered = {k: v for k, v in data.items() if k in known}
        config = OrchestratorConfig.model_validate(filtered)
    except Exception as exc:  # noqa: BLE001
        return (
            CheckResult(
                name="config file",
                ok=False,
                value=str(config_path),
                remediation=f"Config validation failed: {exc}",
            ),
            None,
        )

    return CheckResult(name="config file", ok=True, value=str(config_path)), config


def _check_database(config: OrchestratorConfig | None) -> CheckResult:
    """Check 4: DB writable, schema migrations up to date."""
    # Resolve DB path — prefer config, fall back to default
    if config is not None and config.db_path:
        db_path = config.db_path
    else:
        db_path = Path("./claude-fleet.db")

    if not db_path.exists():
        return CheckResult(
            name="database",
            ok=False,
            value=str(db_path),
            remediation=(
                f"Database '{db_path}' does not exist. "
                "Run 'claude-fleet init' to create it."
            ),
        )

    # Check writable by opening a connection
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode;")
        conn.close()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="database",
            ok=False,
            value=str(db_path),
            remediation=f"Cannot open database: {exc}",
        )

    # Check _applied_migrations table and compare to migrations on disk
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT name FROM _applied_migrations ORDER BY name"
            ).fetchall()
            applied = {row[0] for row in rows}
        except sqlite3.OperationalError:
            conn.close()
            return CheckResult(
                name="database",
                ok=False,
                value=str(db_path),
                remediation=(
                    "Schema bookkeeping table '_applied_migrations' not found. "
                    "Run 'claude-fleet init' to apply migrations."
                ),
            )
        conn.close()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="database",
            ok=False,
            value=str(db_path),
            remediation=f"Failed to query migrations: {exc}",
        )

    # Compare to migrations on disk
    if MIGRATIONS_DIR.is_dir():
        on_disk = {p.name for p in MIGRATIONS_DIR.glob("*.sql")}
        pending = on_disk - applied
        if pending:
            pending_sorted = ", ".join(sorted(pending))
            return CheckResult(
                name="database",
                ok=False,
                value=str(db_path),
                remediation=(
                    f"Pending migrations: {pending_sorted}. "
                    "Run 'claude-fleet init --force' to apply."
                ),
            )

    # Report the latest applied migration in the value
    latest = sorted(applied)[-1] if applied else "none"
    return CheckResult(
        name="database",
        ok=True,
        value=f"{db_path} (schema: {latest})",
    )


def _check_worktree_base(config: OrchestratorConfig | None) -> CheckResult:
    """Check 5: worktrees_root (or deprecated worktree_base) is writable."""
    if config is not None and config.worktrees_root:
        base = config.worktrees_root
    elif config is not None and config.worktree_base:
        # Deprecated alias — still honoured so existing deployments that set
        # worktree_base in their yaml aren't silently broken by doctor.
        base = config.worktree_base
    else:
        base = Path("./worktrees")

    if not base.exists():
        return CheckResult(
            name="worktree base",
            ok=False,
            value=str(base),
            remediation=(
                f"Worktree base directory '{base}' does not exist. "
                "Run 'claude-fleet init' to create it."
            ),
        )

    # Touch a tempfile to verify write access
    try:
        with tempfile.NamedTemporaryFile(dir=base, delete=True):
            pass
        return CheckResult(name="worktree base", ok=True, value=f"{base} (writable)")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="worktree base",
            ok=False,
            value=str(base),
            remediation=f"Cannot write to '{base}': {exc}",
        )


def _check_oauth_token() -> CheckResult:
    """Check 6: CLAUDE_CODE_OAUTH_TOKEN is set."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if token:
        # Truncate for display
        preview = token[:10] + "***" if len(token) > 10 else "***"
        return CheckResult(
            name="OAuth token set",
            ok=True,
            value=f"(truncated: {preview})",
        )
    return CheckResult(
        name="OAuth token set",
        ok=False,
        value=None,
        remediation=(
            "CLAUDE_CODE_OAUTH_TOKEN is not set. "
            "Set it to a valid Claude Code OAuth token."
        ),
    )


def _check_no_anthropic_api_key() -> CheckResult:
    """Check 7: ANTHROPIC_API_KEY must NOT be set."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return CheckResult(
            name="ANTHROPIC_API_KEY",
            ok=False,
            value="(set — must be unset)",
            remediation=(
                "ANTHROPIC_API_KEY must NOT be set when using claude-fleet. "
                "Unset it: `unset ANTHROPIC_API_KEY`"
            ),
        )
    return CheckResult(
        name="ANTHROPIC_API_KEY",
        ok=True,
        value="(unset - good)",
    )


def _check_claude_lb(config: OrchestratorConfig | None) -> CheckResult:
    """Check 8: claude-lb binary present (only if claude_lb.enabled is true in config)."""
    # Access a config attribute that may or may not exist
    lb_enabled = False
    lb_binary = "claude-lb"
    if config is not None:
        lb_cfg = getattr(config, "claude_lb", None)
        if lb_cfg is not None:
            lb_enabled = getattr(lb_cfg, "enabled", False)
            lb_binary = getattr(lb_cfg, "binary", "claude-lb") or "claude-lb"

    if not lb_enabled:
        return CheckResult(
            name="claude-lb",
            ok=None,  # skipped
            value="(extra not installed - skipped)",
        )

    found = shutil.which(lb_binary)
    if found:
        return CheckResult(name="claude-lb", ok=True, value=found)
    return CheckResult(
        name="claude-lb",
        ok=False,
        value=None,
        remediation=(
            f"'{lb_binary}' not found on PATH but claude_lb.enabled is true. "
            "Install claude-lb or set claude_lb.enabled: false in config."
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_checks(config_path: Path = Path("./claude-fleet.yaml")) -> list[CheckResult]:
    """Run all preflight checks and return a list of :class:`CheckResult`.

    All checks run even if an earlier check fails — the caller sees the full
    picture in one pass.

    Args:
        config_path: Path to ``claude-fleet.yaml``.  Used for checks that
            require config values (DB path, worktree base, claude-lb).

    Returns:
        Ordered list of check results (one per check in the spec).
    """
    results: list[CheckResult] = []

    # Checks 1–2 don't need config
    results.append(_check_claude_binary(None))  # config loaded below
    results.append(_check_git())

    # Check 3: load config — pass parsed model to later checks
    config_result, config = _check_config_file(config_path)
    results.append(config_result)

    # Checks 1 (redo with config binary override if config loaded successfully)
    if config is not None:
        binary_override = getattr(config, "claude_binary", "claude")
        if binary_override and binary_override != "claude":
            results[0] = _check_claude_binary(config)

    # Checks 4–8
    results.append(_check_database(config))
    results.append(_check_worktree_base(config))
    results.append(_check_oauth_token())
    results.append(_check_no_anthropic_api_key())
    results.append(_check_claude_lb(config))

    return results


__all__ = ["CheckResult", "run_checks"]
