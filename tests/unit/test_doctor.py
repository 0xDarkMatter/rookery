"""Unit tests for ``claude_fleet.doctor`` and the ``doctor`` CLI command."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_fleet.cli import app
from claude_fleet.doctor import CheckResult, run_checks
from claude_fleet.orchestrator.schema import apply_migrations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_db(path: Path) -> None:
    """Create a valid DB by running all schema migrations (mirrors production state)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(path)


def _make_valid_config(path: Path, db_path: Path, worktree_base: Path) -> None:
    """Write a minimal valid claude-fleet.yaml."""
    path.write_text(
        f"db_path: {db_path}\n"
        f"worktree_base: {worktree_base}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CheckResult model
# ---------------------------------------------------------------------------


class TestCheckResultModel:
    def test_ok_true(self) -> None:
        r = CheckResult(name="test", ok=True, value="val")
        assert r.ok is True

    def test_ok_false(self) -> None:
        r = CheckResult(name="test", ok=False, remediation="fix it")
        assert r.ok is False
        assert r.remediation == "fix it"

    def test_ok_none_skipped(self) -> None:
        r = CheckResult(name="test", ok=None)
        assert r.ok is None

    def test_model_dump_roundtrip(self) -> None:
        r = CheckResult(name="x", ok=True, value="v", remediation=None)
        d = r.model_dump()
        assert d["name"] == "x"
        assert d["ok"] is True
        assert d["value"] == "v"
        assert d["remediation"] is None


# ---------------------------------------------------------------------------
# All-pass scenario
# ---------------------------------------------------------------------------


class TestAllPass:
    """All checks mocked to succeed."""

    def test_run_checks_all_ok(self, tmp_path: Path) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)

        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.42.0", "returncode": 0})(),
            ),
            patch.dict(
                os.environ,
                {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-test-token", "ANTHROPIC_API_KEY": ""},
                clear=False,
            ),
        ):
            # Remove ANTHROPIC_API_KEY if accidentally set in env
            env_backup = os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                results = run_checks(config_path=config_path)
            finally:
                if env_backup is not None:
                    os.environ["ANTHROPIC_API_KEY"] = env_backup

        # Skip check (claude-lb) is ok=None, everything else ok=True
        failed = [r for r in results if r.ok is False]
        assert failed == [], f"Unexpected failures: {[r.name for r in failed]}"

    def test_cli_exits_0_all_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.42.0", "returncode": 0})(),
            ),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-test-token"}),
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = runner.invoke(
                app,
                ["doctor", "--config", str(config_path)],
            )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Individual check failures
# ---------------------------------------------------------------------------


class TestCheckFailures:
    """Each individual check failure: ok=False, remediation set, CLI exits 1."""

    # --- Check 1: claude binary ---

    def test_claude_binary_missing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)

        def _which(b: str) -> str | None:
            return None if b == "claude" else f"/usr/bin/{b}"

        with patch("shutil.which", side_effect=_which):
            results = run_checks(config_path=config_path)

        claude_check = next(r for r in results if r.name == "claude binary")
        assert claude_check.ok is False
        assert claude_check.remediation is not None

    def test_claude_binary_missing_cli_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)
        monkeypatch.chdir(tmp_path)

        def _which(b: str) -> str | None:
            return None if b == "claude" else f"/usr/bin/{b}"

        runner = CliRunner()
        with (
            patch("shutil.which", side_effect=_which),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.42.0", "returncode": 0})(),
            ),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}),
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = runner.invoke(app, ["doctor", "--config", str(config_path)])
        assert result.exit_code == 1

    # --- Check 2: git ---

    def test_git_missing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)

        def _which(b: str) -> str | None:
            return None if b == "git" else f"/usr/bin/{b}"

        with patch("shutil.which", side_effect=_which):
            results = run_checks(config_path=config_path)

        git_check = next(r for r in results if r.name == "git")
        assert git_check.ok is False

    def test_git_too_old(self, tmp_path: Path) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)

        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.4.0", "returncode": 0})(),
            ),
        ):
            results = run_checks(config_path=config_path)

        git_check = next(r for r in results if r.name == "git")
        assert git_check.ok is False
        assert "2.4.0" in (git_check.value or "")

    # --- Check 3: config file ---

    def test_config_missing(self, tmp_path: Path) -> None:
        results = run_checks(config_path=tmp_path / "missing.yaml")
        config_check = next(r for r in results if r.name == "config file")
        assert config_check.ok is False
        assert config_check.remediation is not None

    def test_config_malformed_yaml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(": : : invalid yaml {{{\n", encoding="utf-8")
        results = run_checks(config_path=bad)
        config_check = next(r for r in results if r.name == "config file")
        assert config_check.ok is False

    # --- Check 4: database ---

    def test_db_missing(self, tmp_path: Path) -> None:
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_path = tmp_path / "claude-fleet.yaml"
        missing_db = tmp_path / "nonexistent.db"
        _make_valid_config(config_path, missing_db, worktree_base)

        with patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"):
            results = run_checks(config_path=config_path)

        db_check = next(r for r in results if r.name == "database")
        assert db_check.ok is False

    def test_db_missing_migrations_table(self, tmp_path: Path) -> None:
        """DB exists but _applied_migrations table absent."""
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        db_path = tmp_path / "claude-fleet.db"
        db_path.touch()  # empty db, no tables
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)

        with patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"):
            results = run_checks(config_path=config_path)

        db_check = next(r for r in results if r.name == "database")
        assert db_check.ok is False

    # --- Check 5: worktree base ---

    def test_worktree_base_missing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "claude-fleet.db"
        _make_valid_db(db_path)
        missing_wt = tmp_path / "no-worktrees-here"
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, missing_wt)

        with patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"):
            results = run_checks(config_path=config_path)

        wt_check = next(r for r in results if r.name == "worktree base")
        assert wt_check.ok is False

    # --- Check 6: OAuth token ---

    def test_oauth_token_missing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)

        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.42.0", "returncode": 0})(),
            ),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            results = run_checks(config_path=config_path)

        token_check = next(r for r in results if r.name == "OAuth token set")
        assert token_check.ok is False
        assert token_check.remediation is not None

    # --- Check 7: ANTHROPIC_API_KEY must NOT be set ---

    def test_anthropic_api_key_set_fails(self, tmp_path: Path) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)

        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.42.0", "returncode": 0})(),
            ),
            patch.dict(
                os.environ,
                {"ANTHROPIC_API_KEY": "sk-bad-key", "CLAUDE_CODE_OAUTH_TOKEN": "tok"},
            ),
        ):
            results = run_checks(config_path=config_path)

        api_check = next(r for r in results if r.name == "ANTHROPIC_API_KEY")
        assert api_check.ok is False
        assert api_check.remediation is not None

    def test_anthropic_api_key_unset_passes(self, tmp_path: Path) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)

        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.42.0", "returncode": 0})(),
            ),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}),
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            results = run_checks(config_path=config_path)

        api_check = next(r for r in results if r.name == "ANTHROPIC_API_KEY")
        assert api_check.ok is True


# ---------------------------------------------------------------------------
# Skipped checks (claude-lb when disabled)
# ---------------------------------------------------------------------------


class TestSkippedChecks:
    def test_claude_lb_skipped_when_disabled(self, tmp_path: Path) -> None:
        """claude-lb check is skipped when config does not enable it."""
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)

        with patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"):
            results = run_checks(config_path=config_path)

        lb_check = next(r for r in results if r.name == "claude-lb")
        assert lb_check.ok is None

    def test_skipped_check_does_not_cause_exit_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Skipped checks are neutral — they must not trigger exit 1."""
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.42.0", "returncode": 0})(),
            ),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}),
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = runner.invoke(app, ["doctor", "--config", str(config_path)])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# JSON mode
# ---------------------------------------------------------------------------


class TestJsonMode:
    def test_json_mode_parseable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.42.0", "returncode": 0})(),
            ),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}),
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = runner.invoke(
                app,
                ["doctor", "--config", str(config_path), "--json"],
            )

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert len(parsed) > 0
        # Each item must have the expected keys
        for item in parsed:
            assert "name" in item
            assert "ok" in item
            assert "value" in item
            assert "remediation" in item

    def test_json_mode_exits_1_on_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--json mode exits 1 when a check fails."""
        config_path = tmp_path / "missing.yaml"  # config won't exist
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with patch("shutil.which", return_value=None):  # all binaries missing
            result = runner.invoke(
                app,
                ["doctor", "--config", str(config_path), "--json"],
            )

        assert result.exit_code == 1
        parsed = json.loads(result.output)
        failed = [r for r in parsed if r["ok"] is False]
        assert len(failed) > 0

    def test_json_mode_structure_with_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Skipped checks appear in JSON output with ok=null."""
        db_path = tmp_path / "claude-fleet.db"
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        _make_valid_db(db_path)
        config_path = tmp_path / "claude-fleet.yaml"
        _make_valid_config(config_path, db_path, worktree_base)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch(
                "subprocess.run",
                return_value=type("R", (), {"stdout": "git version 2.42.0", "returncode": 0})(),
            ),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}),
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = runner.invoke(
                app,
                ["doctor", "--config", str(config_path), "--json"],
            )

        parsed = json.loads(result.output)
        skipped = [r for r in parsed if r["ok"] is None]
        assert len(skipped) >= 1  # claude-lb is skipped
        skipped_names = {r["name"] for r in skipped}
        assert "claude-lb" in skipped_names


# ---------------------------------------------------------------------------
# Collect all results (no stopping on first failure)
# ---------------------------------------------------------------------------


class TestCollectAll:
    """Even with multiple failures, run_checks returns all N results."""

    def test_returns_8_results(self, tmp_path: Path) -> None:
        # Config missing → still get 8 results
        results = run_checks(config_path=tmp_path / "absent.yaml")
        assert len(results) == 8

    def test_does_not_stop_on_first_failure(self, tmp_path: Path) -> None:
        """All-missing scenario: still get results for every check."""
        with patch("shutil.which", return_value=None):
            results = run_checks(config_path=tmp_path / "absent.yaml")
        # git and claude binary should both fail
        names = [r.name for r in results]
        assert "claude binary" in names
        assert "git" in names
        assert len(results) == 8
