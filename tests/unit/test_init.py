"""Unit tests for ``rookery.init.cmd_init`` and the CLI ``init`` command."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rookery.cli import app
from rookery.init import InitError, cmd_init
from rookery.orchestrator.config import OrchestratorConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_created(target: Path) -> None:
    """Assert every artefact the spec requires exists under *target*."""
    assert (target / "rookery.yaml").is_file(), "rookery.yaml missing"
    assert (target / "rookery.db").is_file(), "rookery.db missing"
    assert (target / "parcels").is_dir(), "parcels/ missing"
    assert (target / "worktrees").is_dir(), "worktrees/ missing"
    assert (target / "worktrees" / ".gitignore").is_file(), "worktrees/.gitignore missing"


# ---------------------------------------------------------------------------
# Core function tests
# ---------------------------------------------------------------------------


class TestFreshDirectory:
    """Fresh empty directory — all files should be created."""

    def test_all_artefacts_created(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        _all_created(tmp_path)

    def test_parcels_gitkeep_present(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        assert (tmp_path / "parcels" / ".gitkeep").is_file()

    def test_worktrees_gitignore_content(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        content = (tmp_path / "worktrees" / ".gitignore").read_text()
        assert content.strip() == "*"

    def test_root_gitignore_entries(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        text = (tmp_path / ".gitignore").read_text()
        for entry in ("rookery.db", "rookery.db-wal", "*.log", "worktrees/"):
            assert entry in text, f"missing gitignore entry: {entry}"


class TestExistingConfig:
    """If rookery.yaml already exists, refuse unless --force."""

    def test_refuses_without_force(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        with pytest.raises(InitError, match="already exists"):
            cmd_init(target_dir=tmp_path, force=False)

    def test_does_not_modify_on_refusal(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        # Write a sentinel so we can detect unwanted overwrite
        (tmp_path / "rookery.yaml").write_text("sentinel: true\n")
        with pytest.raises(InitError):
            cmd_init(target_dir=tmp_path, force=False)
        # File should still contain our sentinel
        assert (tmp_path / "rookery.yaml").read_text() == "sentinel: true\n"


class TestForceOverwrite:
    """With force=True, should overwrite config and re-run migrations."""

    def test_overwrites_yaml(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        (tmp_path / "rookery.yaml").write_text("sentinel: true\n")
        cmd_init(target_dir=tmp_path, force=True)
        text = (tmp_path / "rookery.yaml").read_text()
        assert "sentinel" not in text

    def test_all_artefacts_still_present_after_force(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        cmd_init(target_dir=tmp_path, force=True)
        _all_created(tmp_path)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    """Generated YAML must parse and validate against OrchestratorConfig."""

    def test_yaml_is_valid(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        raw = (tmp_path / "rookery.yaml").read_text()
        data = yaml.safe_load(raw)
        assert isinstance(data, dict), "top-level YAML must be a mapping"

    def test_config_model_validates(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        raw = (tmp_path / "rookery.yaml").read_text()
        data = yaml.safe_load(raw)
        # Drop keys that belong to the API doc but not the current model, then validate
        # We only validate against known model fields
        known_fields = set(OrchestratorConfig.model_fields)
        filtered = {k: v for k, v in data.items() if k in known_fields}
        config = OrchestratorConfig.model_validate(filtered)
        assert config is not None


# ---------------------------------------------------------------------------
# Database migrations
# ---------------------------------------------------------------------------


class TestDatabaseMigrations:
    """Created DB should have migrations applied."""

    def test_migrations_table_populated(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        db = tmp_path / "rookery.db"
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT name FROM _applied_migrations ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) >= 1, "expected at least one migration row"

    def test_db_file_created(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        assert (tmp_path / "rookery.db").stat().st_size > 0


# ---------------------------------------------------------------------------
# Idempotent .gitignore
# ---------------------------------------------------------------------------


class TestGitignoreIdempotency:
    """Running init twice must not duplicate .gitignore entries."""

    def test_no_duplicate_entries(self, tmp_path: Path) -> None:
        cmd_init(target_dir=tmp_path)
        cmd_init(target_dir=tmp_path, force=True)
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        stripped = [ln.strip() for ln in lines if ln.strip()]
        assert len(stripped) == len(set(stripped)), (
            "Duplicate entries found in .gitignore after force re-init"
        )

    def test_existing_gitignore_entries_preserved(self, tmp_path: Path) -> None:
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
        cmd_init(target_dir=tmp_path)
        text = gitignore.read_text()
        assert "*.pyc" in text
        assert "__pycache__/" in text
        assert "rookery.db" in text

    def test_pre_existing_entry_not_duplicated(self, tmp_path: Path) -> None:
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("rookery.db\n", encoding="utf-8")
        cmd_init(target_dir=tmp_path)
        lines = gitignore.read_text().splitlines()
        count = lines.count("rookery.db")
        assert count == 1, f"expected 1 occurrence, got {count}"


# ---------------------------------------------------------------------------
# CLI tests (CliRunner)
# ---------------------------------------------------------------------------


class TestCLI:
    """Test the ``init`` command via Typer's CliRunner."""

    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_happy_path(self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output
        assert "rookery.yaml" in result.output
        assert "rookery.db" in result.output
        _all_created(tmp_path)

    def test_refuses_if_exists(self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["init"])
        assert result.exit_code != 0

    def test_force_flag_overwrites(self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init"])
        (tmp_path / "rookery.yaml").write_text("sentinel: true\n")
        result = runner.invoke(app, ["init", "--force"])
        assert result.exit_code == 0, result.output
        assert "sentinel" not in (tmp_path / "rookery.yaml").read_text()
