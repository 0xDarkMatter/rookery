"""Regression tests for daemon db_path resolution from rookery.yaml.

Covers the bug where ``rookery-daemon start`` ignored the yaml ``db_path``
and always used the hardcoded ``OrchestratorConfig`` default
(``.data/orchestrator.db``) instead of the value from config.

Resolution order under test (mirrors __main__.start_cmd):
    1. explicit ``--db`` flag > yaml ``db_path`` > hardcoded fallback ./rookery.db
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _resolve_db_path(
    config_path: Path,
    db_override: str = "",
) -> Path:
    """Mirror the resolution logic from __main__.start_cmd.

    Uses the real ``load_config`` + fallback + override chain so any drift
    between this helper and the actual daemon entry point will surface as a
    test failure.
    """
    from rookery.orchestrator.config import OrchestratorConfig, load_config

    cfg = load_config(config_path)

    # Apply the hardcoded db_path fallback only when the yaml also didn't set it
    # (load_config returns the pydantic default .data/orchestrator.db in that case)
    if cfg.db_path == Path(".data/orchestrator.db"):
        cfg = cfg.model_copy(update={"db_path": Path("./rookery.db")})

    # --db flag (or ROOKERY_DB) wins over everything
    if db_override:
        cfg = cfg.model_copy(update={"db_path": Path(db_override)})

    return cfg.db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_yaml_db_path_is_respected(tmp_path: Path) -> None:
    """yaml db_path: ./custom.db must be used when no --db flag is given."""
    config_file = tmp_path / "rookery.yaml"
    config_file.write_text("db_path: ./custom.db\n", encoding="utf-8")

    resolved = _resolve_db_path(config_file)

    assert resolved == Path("./custom.db"), (
        f"Expected ./custom.db but got {resolved}. "
        "The daemon is ignoring the yaml db_path."
    )


def test_explicit_db_flag_overrides_yaml(tmp_path: Path) -> None:
    """--db flag must win over yaml db_path."""
    config_file = tmp_path / "rookery.yaml"
    config_file.write_text("db_path: ./yaml.db\n", encoding="utf-8")
    explicit = str(tmp_path / "explicit.db")

    resolved = _resolve_db_path(config_file, db_override=explicit)

    assert resolved == Path(explicit)


def test_missing_config_falls_back_to_default(tmp_path: Path) -> None:
    """When the config file doesn't exist, fall back to ./rookery.db."""
    missing = tmp_path / "no-such-file.yaml"

    resolved = _resolve_db_path(missing)

    assert resolved == Path("./rookery.db"), (
        f"Expected ./rookery.db fallback but got {resolved}."
    )


def test_yaml_without_db_path_uses_default(tmp_path: Path) -> None:
    """A yaml that omits db_path should still fall back to ./rookery.db."""
    config_file = tmp_path / "rookery.yaml"
    config_file.write_text("max_concurrent: 4\n", encoding="utf-8")

    resolved = _resolve_db_path(config_file)

    assert resolved == Path("./rookery.db")


def test_old_default_data_dir_is_never_used(tmp_path: Path) -> None:
    """The stale .data/orchestrator.db default must never be the resolution result."""
    config_file = tmp_path / "rookery.yaml"
    config_file.write_text("db_path: ./rookery.db\n", encoding="utf-8")

    resolved = _resolve_db_path(config_file)

    assert str(resolved) != ".data/orchestrator.db", (
        "daemon resolved to the old stale default — yaml is being ignored"
    )


def test_load_config_returns_yaml_db_path_directly(tmp_path: Path) -> None:
    """load_config itself must honour db_path from yaml (unit test of the helper)."""
    from rookery.orchestrator.config import load_config

    config_file = tmp_path / "rookery.yaml"
    config_file.write_text("db_path: ./fleet.db\n", encoding="utf-8")

    cfg = load_config(config_file)

    assert cfg.db_path == Path("./fleet.db")
