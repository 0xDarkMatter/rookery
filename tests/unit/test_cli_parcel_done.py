"""Tests for ``rookery parcel done`` and ``rookery parcel progress`` CLI.

The two helpers form the v0.3 worker-reporting protocol: workers invoke them
from inside the parcel worktree, and they write directly to the queue DB via
env vars (ROOKERY_DB / ROOKERY_PARCEL_ID / ROOKERY_PARCEL_ATTEMPT) injected
by the daemon at spawn time.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rookery.cli import app
from rookery.orchestrator.orchestrator import Orchestrator


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def orch_with_job(tmp_path: Path) -> Generator[tuple[Orchestrator, Path], None, None]:
    """Orchestrator + DB path with one enqueued + claimed job ready to receive a verdict."""
    db_path = tmp_path / "rookery.db"
    o = Orchestrator(db_path, lease_ttl_s=60)
    o.enqueue("test-parcel", "parcels/test.md")
    yield o, db_path
    o.close()


# ---------------------------------------------------------------------------
# parcel done
# ---------------------------------------------------------------------------


class TestParcelDoneHappyPath:
    def test_minimal_invocation_writes_row(
        self,
        runner: CliRunner,
        orch_with_job: tuple[Orchestrator, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, db_path = orch_with_job
        monkeypatch.setenv("ROOKERY_DB", str(db_path))
        monkeypatch.setenv("ROOKERY_PARCEL_ID", "test-parcel")
        monkeypatch.setenv("ROOKERY_PARCEL_ATTEMPT", "1")

        result = runner.invoke(
            app,
            ["parcel", "done", "--verdict", "PASS", "--summary", "all green"],
        )

        assert result.exit_code == 0, result.output
        assert "verdict PASS recorded" in result.output

        row = orch.read_parcel_result("test-parcel", attempt=1)
        assert row is not None
        assert row["verdict"] == "PASS"
        assert row["summary"] == "all green"
        assert row["reported_via"] == "cli"

    def test_full_metadata_round_trip(
        self,
        runner: CliRunner,
        orch_with_job: tuple[Orchestrator, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, db_path = orch_with_job
        monkeypatch.setenv("ROOKERY_DB", str(db_path))
        monkeypatch.setenv("ROOKERY_PARCEL_ID", "test-parcel")
        monkeypatch.setenv("ROOKERY_PARCEL_ATTEMPT", "1")

        result = runner.invoke(
            app,
            [
                "parcel", "done",
                "--verdict", "PASS_WITH_WARNINGS",
                "--summary", "OAuth added",
                "--tokens-in", "12500",
                "--tokens-out", "3200",
                "--duration-s", "187.5",
                "--tests-passed", "42",
                "--tests-failed", "1",
                "--files-changed", "3",
                "--detail", "## Detail\n\nLong story",
            ],
        )

        assert result.exit_code == 0, result.output
        row = orch.read_parcel_result("test-parcel", attempt=1)
        assert row is not None
        assert row["verdict"] == "PASS_WITH_WARNINGS"
        assert row["tokens_in"] == 12500
        assert row["tokens_out"] == 3200
        assert row["duration_s"] == pytest.approx(187.5)
        assert row["tests_passed"] == 42
        assert row["tests_failed"] == 1
        assert row["files_changed"] == 3
        assert row["detail_md"] == "## Detail\n\nLong story"

    def test_verdict_lowercase_normalised(
        self,
        runner: CliRunner,
        orch_with_job: tuple[Orchestrator, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Workers may emit lowercase tokens; the helper canonicalises to upper."""
        orch, db_path = orch_with_job
        monkeypatch.setenv("ROOKERY_DB", str(db_path))
        monkeypatch.setenv("ROOKERY_PARCEL_ID", "test-parcel")
        monkeypatch.setenv("ROOKERY_PARCEL_ATTEMPT", "1")

        result = runner.invoke(
            app, ["parcel", "done", "--verdict", "pass", "--summary", "x"]
        )
        assert result.exit_code == 0
        row = orch.read_parcel_result("test-parcel", attempt=1)
        assert row is not None
        assert row["verdict"] == "PASS"

    def test_detail_file_read(
        self,
        runner: CliRunner,
        orch_with_job: tuple[Orchestrator, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, db_path = orch_with_job
        detail_path = tmp_path / "detail.md"
        detail_path.write_text("# Body from file\n\nlong narrative", encoding="utf-8")

        monkeypatch.setenv("ROOKERY_DB", str(db_path))
        monkeypatch.setenv("ROOKERY_PARCEL_ID", "test-parcel")
        monkeypatch.setenv("ROOKERY_PARCEL_ATTEMPT", "1")

        result = runner.invoke(
            app,
            ["parcel", "done", "--verdict", "PASS", "--summary", "x",
             "--detail-file", str(detail_path)],
        )
        assert result.exit_code == 0
        row = orch.read_parcel_result("test-parcel", attempt=1)
        assert row is not None
        assert row["detail_md"] == "# Body from file\n\nlong narrative"

    def test_write_marker_file_dual_write(
        self,
        runner: CliRunner,
        orch_with_job: tuple[Orchestrator, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, db_path = orch_with_job
        worktree = tmp_path / "wt"
        worktree.mkdir()

        monkeypatch.setenv("ROOKERY_DB", str(db_path))
        monkeypatch.setenv("ROOKERY_PARCEL_ID", "test-parcel")
        monkeypatch.setenv("ROOKERY_PARCEL_ATTEMPT", "1")
        monkeypatch.setenv("ROOKERY_WORKTREE", str(worktree))

        result = runner.invoke(
            app,
            ["parcel", "done", "--verdict", "PASS", "--summary", "x",
             "--write-marker-file"],
        )
        assert result.exit_code == 0
        marker = worktree / "PARCEL_DONE-test-parcel.md"
        assert marker.exists()
        body = marker.read_text(encoding="utf-8")
        assert "Verdict: PASS" in body
        assert "## Summary" in body


class TestParcelDoneErrors:
    def test_missing_env_helpful_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ROOKERY_DB", raising=False)
        monkeypatch.delenv("ROOKERY_PARCEL_ID", raising=False)
        monkeypatch.delenv("ROOKERY_PARCEL_ATTEMPT", raising=False)

        result = runner.invoke(
            app, ["parcel", "done", "--verdict", "PASS", "--summary", "x"]
        )
        assert result.exit_code == 2
        assert "ROOKERY_DB" in result.output
        assert "ROOKERY_PARCEL_ID" in result.output
        # Helpful remediation is shown
        assert "rookery parcel done" in result.output

    def test_invalid_verdict_rejected(
        self,
        runner: CliRunner,
        orch_with_job: tuple[Orchestrator, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _orch, db_path = orch_with_job
        monkeypatch.setenv("ROOKERY_DB", str(db_path))
        monkeypatch.setenv("ROOKERY_PARCEL_ID", "test-parcel")
        monkeypatch.setenv("ROOKERY_PARCEL_ATTEMPT", "1")

        result = runner.invoke(
            app, ["parcel", "done", "--verdict", "WHATEVER", "--summary", "x"]
        )
        assert result.exit_code == 2
        assert "verdict must be one of" in result.output

    def test_detail_and_detail_file_mutually_exclusive(
        self,
        runner: CliRunner,
        orch_with_job: tuple[Orchestrator, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _orch, db_path = orch_with_job
        df = tmp_path / "d.md"
        df.write_text("x", encoding="utf-8")
        monkeypatch.setenv("ROOKERY_DB", str(db_path))
        monkeypatch.setenv("ROOKERY_PARCEL_ID", "test-parcel")
        monkeypatch.setenv("ROOKERY_PARCEL_ATTEMPT", "1")

        result = runner.invoke(
            app,
            ["parcel", "done", "--verdict", "PASS", "--summary", "x",
             "--detail", "inline", "--detail-file", str(df)],
        )
        assert result.exit_code == 2
        assert "use --detail-file OR --detail" in result.output


# ---------------------------------------------------------------------------
# parcel progress
# ---------------------------------------------------------------------------


class TestParcelProgress:
    def test_basic_event_appended(
        self,
        runner: CliRunner,
        orch_with_job: tuple[Orchestrator, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, db_path = orch_with_job
        monkeypatch.setenv("ROOKERY_DB", str(db_path))
        monkeypatch.setenv("ROOKERY_PARCEL_ID", "test-parcel")
        monkeypatch.setenv("ROOKERY_PARCEL_ATTEMPT", "1")

        result = runner.invoke(
            app, ["parcel", "progress", "Implemented OAuth"]
        )
        assert result.exit_code == 0, result.output
        events = orch.read_parcel_events("test-parcel")
        assert len(events) == 1
        assert events[0]["label"] == "Implemented OAuth"
        assert events[0]["event_type"] == "progress"

    def test_phase_recorded_in_payload(
        self,
        runner: CliRunner,
        orch_with_job: tuple[Orchestrator, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, db_path = orch_with_job
        monkeypatch.setenv("ROOKERY_DB", str(db_path))
        monkeypatch.setenv("ROOKERY_PARCEL_ID", "test-parcel")
        monkeypatch.setenv("ROOKERY_PARCEL_ATTEMPT", "1")

        result = runner.invoke(
            app,
            ["parcel", "progress", "Stuck on tests", "--phase", "stuck",
             "--detail", "test_x is flaky"],
        )
        assert result.exit_code == 0
        events = orch.read_parcel_events("test-parcel")
        assert len(events) == 1
        assert events[0]["detail"] == "test_x is flaky"
        assert '"phase": "stuck"' in events[0]["payload_json"]

    def test_missing_env_helpful_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ROOKERY_DB", raising=False)
        monkeypatch.delenv("ROOKERY_PARCEL_ID", raising=False)

        result = runner.invoke(app, ["parcel", "progress", "step"])
        assert result.exit_code == 2
        assert "ROOKERY_DB" in result.output


class TestParcelHelpText:
    """Smoke tests that --help renders without crashing."""

    def test_done_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["parcel", "done", "--help"])
        assert result.exit_code == 0
        assert "verdict" in result.output.lower()

    def test_progress_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["parcel", "progress", "--help"])
        assert result.exit_code == 0
        assert "progress" in result.output.lower()
