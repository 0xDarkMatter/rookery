"""Unit tests for the G4 pluggable verdict adapter system.

Covers:
- MarkerFileAdapter: positive detection, no-detection, malformed, legacy fallback
- JsonResultAdapter: positive, missing, malformed JSON
- ExitCodeAdapter: exit 0=PASS, non-zero=BLOCK, None=still running
- Registry: correct adapter for known names, UnknownVerdictAdapter for unknown
- Per-parcel override: frontmatter verdict_adapter stored + resolved via daemon
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_fleet.adapters.base import VerdictAdapter, VerdictResult
from claude_fleet.adapters.exit_code import ExitCodeAdapter
from claude_fleet.adapters.json_result import JsonResultAdapter
from claude_fleet.adapters.marker_file import MarkerFileAdapter
from claude_fleet.adapters.registry import (
    VERDICT_ADAPTERS,
    UnknownVerdictAdapter,
    get_verdict_adapter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# MarkerFileAdapter
# ---------------------------------------------------------------------------


class TestMarkerFileAdapter:
    def test_positive_detection_pass(self, tmp_path: Path) -> None:
        job_id = "my-task"
        _write(
            tmp_path / f"PARCEL_DONE-{job_id}.md",
            "# PARCEL_DONE: my-task\n\nVerdict: PASS\n\n## Summary\nAll good.\n",
        )
        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, job_id)

        assert result is not None
        assert result.verdict == "PASS"
        assert result.summary == "All good."

    def test_positive_detection_block(self, tmp_path: Path) -> None:
        job_id = "my-task"
        _write(
            tmp_path / f"PARCEL_DONE-{job_id}.md",
            "Verdict: BLOCK\n\n## Summary\nCould not complete.\n",
        )
        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, job_id)

        assert result is not None
        assert result.verdict == "BLOCK"
        assert result.summary == "Could not complete."

    def test_no_detection_file_missing(self, tmp_path: Path) -> None:
        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, "absent-job")

        assert result is None  # worker still running

    def test_malformed_no_verdict_line(self, tmp_path: Path) -> None:
        job_id = "bad-job"
        _write(
            tmp_path / f"PARCEL_DONE-{job_id}.md",
            "# PARCEL_DONE: bad-job\n\nNo verdict line here.\n",
        )
        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, job_id)

        assert result is not None
        assert result.verdict == "UNKNOWN"
        assert "malformed" in result.detail.get("error", "")

    def test_malformed_unrecognised_token(self, tmp_path: Path) -> None:
        job_id = "odd-job"
        _write(
            tmp_path / f"PARCEL_DONE-{job_id}.md",
            "Verdict: MAYBE\n",
        )
        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, job_id)

        # Unrecognised token → ValueError inside → UNKNOWN
        assert result is not None
        assert result.verdict == "UNKNOWN"

    def test_pass_with_warnings(self, tmp_path: Path) -> None:
        job_id = "warn-job"
        _write(
            tmp_path / f"PARCEL_DONE-{job_id}.md",
            "**Verdict:** PASS_WITH_WARNINGS\n",
        )
        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, job_id)

        assert result is not None
        assert result.verdict == "PASS_WITH_WARNINGS"

    def test_singular_warning_normalised(self, tmp_path: Path) -> None:
        """PASS_WITH_WARNING (singular) must be canonicalised to PASS_WITH_WARNINGS."""
        job_id = "warn-job"
        _write(
            tmp_path / f"PARCEL_DONE-{job_id}.md",
            "Verdict: PASS_WITH_WARNING\n",
        )
        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, job_id)

        assert result is not None
        assert result.verdict == "PASS_WITH_WARNINGS"

    def test_legacy_fallback_parcel_done_no_suffix(self, tmp_path: Path) -> None:
        """When id-suffixed file is absent, fall back to PARCEL_DONE.md."""
        _write(tmp_path / "PARCEL_DONE.md", "Verdict: PASS\n")
        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, "some-job-id")

        assert result is not None
        assert result.verdict == "PASS"
        assert "PARCEL_DONE.md" in result.detail.get("marker_file", "")

    def test_id_specific_file_preferred_over_legacy(self, tmp_path: Path) -> None:
        """Id-specific file wins over legacy PARCEL_DONE.md when both exist."""
        job_id = "specific-job"
        _write(tmp_path / f"PARCEL_DONE-{job_id}.md", "Verdict: PASS\n")
        _write(tmp_path / "PARCEL_DONE.md", "Verdict: BLOCK\n")  # should be ignored

        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, job_id)

        assert result is not None
        assert result.verdict == "PASS"

    def test_detail_contains_marker_file_path(self, tmp_path: Path) -> None:
        job_id = "path-check"
        done_file = tmp_path / f"PARCEL_DONE-{job_id}.md"
        _write(done_file, "Verdict: PASS\n")

        adapter = MarkerFileAdapter()
        result = adapter.detect(tmp_path, job_id)

        assert result is not None
        assert str(done_file) == result.detail.get("marker_file")


# ---------------------------------------------------------------------------
# JsonResultAdapter
# ---------------------------------------------------------------------------


class TestJsonResultAdapter:
    def test_positive_pass(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "result.json",
            json.dumps({"verdict": "PASS", "summary": "Tests green", "detail": {"coverage": 95}}),
        )
        adapter = JsonResultAdapter()
        result = adapter.detect(tmp_path, "job-1")

        assert result is not None
        assert result.verdict == "PASS"
        assert result.summary == "Tests green"
        assert result.detail.get("coverage") == 95

    def test_positive_block(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "result.json",
            json.dumps({"verdict": "BLOCK", "summary": "Build failed"}),
        )
        adapter = JsonResultAdapter()
        result = adapter.detect(tmp_path, "job-2")

        assert result is not None
        assert result.verdict == "BLOCK"
        assert result.summary == "Build failed"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        adapter = JsonResultAdapter()
        result = adapter.detect(tmp_path, "job-3")

        assert result is None

    def test_malformed_json(self, tmp_path: Path) -> None:
        _write(tmp_path / "result.json", "{not valid json")
        adapter = JsonResultAdapter()
        result = adapter.detect(tmp_path, "job-4")

        assert result is not None
        assert result.verdict == "UNKNOWN"
        assert "invalid JSON" in result.detail.get("error", "")

    def test_missing_verdict_key(self, tmp_path: Path) -> None:
        _write(tmp_path / "result.json", json.dumps({"summary": "no verdict"}))
        adapter = JsonResultAdapter()
        result = adapter.detect(tmp_path, "job-5")

        assert result is not None
        assert result.verdict == "UNKNOWN"
        assert "verdict" in result.detail.get("error", "")

    def test_unknown_verdict_value(self, tmp_path: Path) -> None:
        _write(tmp_path / "result.json", json.dumps({"verdict": "MAYBE"}))
        adapter = JsonResultAdapter()
        result = adapter.detect(tmp_path, "job-6")

        assert result is not None
        assert result.verdict == "UNKNOWN"

    def test_not_a_json_object(self, tmp_path: Path) -> None:
        _write(tmp_path / "result.json", json.dumps([1, 2, 3]))
        adapter = JsonResultAdapter()
        result = adapter.detect(tmp_path, "job-7")

        assert result is not None
        assert result.verdict == "UNKNOWN"
        assert "JSON object" in result.detail.get("error", "")

    def test_result_file_path_in_detail(self, tmp_path: Path) -> None:
        _write(tmp_path / "result.json", json.dumps({"verdict": "PASS"}))
        adapter = JsonResultAdapter()
        result = adapter.detect(tmp_path, "job-8")

        assert result is not None
        assert "result.json" in result.detail.get("result_file", "")

    def test_pass_with_warnings(self, tmp_path: Path) -> None:
        _write(tmp_path / "result.json", json.dumps({"verdict": "PASS_WITH_WARNINGS"}))
        adapter = JsonResultAdapter()
        result = adapter.detect(tmp_path, "job-9")

        assert result is not None
        assert result.verdict == "PASS_WITH_WARNINGS"


# ---------------------------------------------------------------------------
# ExitCodeAdapter
# ---------------------------------------------------------------------------


class TestExitCodeAdapter:
    def test_exit_zero_is_pass(self, tmp_path: Path) -> None:
        adapter = ExitCodeAdapter(exit_code_fn=lambda _: 0)
        result = adapter.detect(tmp_path, "job-1")

        assert result is not None
        assert result.verdict == "PASS"
        assert result.detail.get("exit_code") == 0

    def test_nonzero_exit_is_block(self, tmp_path: Path) -> None:
        adapter = ExitCodeAdapter(exit_code_fn=lambda _: 1)
        result = adapter.detect(tmp_path, "job-2")

        assert result is not None
        assert result.verdict == "BLOCK"
        assert result.detail.get("exit_code") == 1

    def test_exit_code_127_is_block(self, tmp_path: Path) -> None:
        adapter = ExitCodeAdapter(exit_code_fn=lambda _: 127)
        result = adapter.detect(tmp_path, "job-3")

        assert result is not None
        assert result.verdict == "BLOCK"

    def test_none_means_still_running(self, tmp_path: Path) -> None:
        adapter = ExitCodeAdapter(exit_code_fn=lambda _: None)
        result = adapter.detect(tmp_path, "job-4")

        assert result is None  # worker still alive

    def test_mock_based_runner_still_alive(self, tmp_path: Path) -> None:
        """Use MagicMock to simulate a runner that hasn't exited yet."""
        fn = MagicMock(return_value=None)
        adapter = ExitCodeAdapter(exit_code_fn=fn)
        result = adapter.detect(tmp_path, "my-job")

        assert result is None
        fn.assert_called_once_with("my-job")

    def test_mock_based_runner_exited(self, tmp_path: Path) -> None:
        """Use MagicMock to simulate clean exit."""
        fn = MagicMock(return_value=0)
        adapter = ExitCodeAdapter(exit_code_fn=fn)
        result = adapter.detect(tmp_path, "my-job")

        assert result is not None
        assert result.verdict == "PASS"
        fn.assert_called_once_with("my-job")

    def test_job_id_forwarded_to_fn(self, tmp_path: Path) -> None:
        captured: list[str] = []

        def record_fn(job_id: str) -> int | None:
            captured.append(job_id)
            return 0

        adapter = ExitCodeAdapter(exit_code_fn=record_fn)
        adapter.detect(tmp_path, "specific-job-id")

        assert captured == ["specific-job-id"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_marker_file_registered(self) -> None:
        assert "marker-file" in VERDICT_ADAPTERS
        assert VERDICT_ADAPTERS["marker-file"] is MarkerFileAdapter

    def test_exit_code_registered(self) -> None:
        assert "exit-code" in VERDICT_ADAPTERS
        assert VERDICT_ADAPTERS["exit-code"] is ExitCodeAdapter

    def test_json_result_registered(self) -> None:
        assert "json-result" in VERDICT_ADAPTERS
        assert VERDICT_ADAPTERS["json-result"] is JsonResultAdapter

    def test_get_verdict_adapter_marker_file(self) -> None:
        adapter = get_verdict_adapter("marker-file")
        assert isinstance(adapter, MarkerFileAdapter)

    def test_get_verdict_adapter_json_result(self) -> None:
        adapter = get_verdict_adapter("json-result")
        assert isinstance(adapter, JsonResultAdapter)

    def test_get_verdict_adapter_unknown_raises(self) -> None:
        with pytest.raises(UnknownVerdictAdapter) as exc_info:
            get_verdict_adapter("does-not-exist")
        assert "does-not-exist" in str(exc_info.value)
        assert exc_info.value.name == "does-not-exist"

    def test_unknown_verdict_adapter_is_exception(self) -> None:
        err = UnknownVerdictAdapter("my-adapter")
        assert isinstance(err, Exception)
        assert "my-adapter" in str(err)

    def test_exit_code_not_zero_arg_constructible(self) -> None:
        """ExitCodeAdapter requires exit_code_fn; registry instantiation raises TypeError."""
        with pytest.raises(TypeError):
            get_verdict_adapter("exit-code")

    def test_adapter_abc_not_instantiable(self) -> None:
        with pytest.raises(TypeError):
            VerdictAdapter()  # type: ignore[abstract]

    def test_entry_points_missing_group_does_not_raise(self) -> None:
        """get_verdict_adapter must not crash when entry_points has no matching group."""
        # Simulate entry_points returning empty for the group — no plugin installed.
        # Unknown name should still raise UnknownVerdictAdapter, not ImportError.
        with pytest.raises(UnknownVerdictAdapter):
            get_verdict_adapter("plugin-that-doesnt-exist")


# ---------------------------------------------------------------------------
# Per-parcel override
# ---------------------------------------------------------------------------


class TestPerParcelOverride:
    """Tests that enqueue stores verdict_adapter and daemon resolves it."""

    def test_enqueue_stores_verdict_adapter(self, tmp_path: Path) -> None:
        """Enqueuing with verdict_adapter stores it on the Job."""
        from claude_fleet.orchestrator.orchestrator import Orchestrator

        db = tmp_path / "q.db"
        orch = Orchestrator(db_path=db)
        try:
            job = orch.enqueue(
                "task-1",
                "parcels/task-1.md",
                verdict_adapter="json-result",
            )
            assert job.verdict_adapter == "json-result"

            # Re-read from DB to confirm persistence.
            fetched = orch.status("task-1")
            assert fetched.verdict_adapter == "json-result"
        finally:
            orch.close()

    def test_enqueue_default_verdict_adapter_is_none(self, tmp_path: Path) -> None:
        """verdict_adapter defaults to NULL (None) when not specified."""
        from claude_fleet.orchestrator.orchestrator import Orchestrator

        db = tmp_path / "q.db"
        orch = Orchestrator(db_path=db)
        try:
            job = orch.enqueue("task-2", "parcels/task-2.md")
            assert job.verdict_adapter is None
        finally:
            orch.close()

    def test_daemon_resolves_per_job_override(self, tmp_path: Path) -> None:
        """Daemon._get_verdict_adapter_for_job returns job-specific adapter."""
        import asyncio

        from claude_fleet.orchestrator.daemon import Daemon
        from claude_fleet.orchestrator.orchestrator import Orchestrator
        from tests.unit.orchestrator.fakes import FakeBackend

        db = tmp_path / "q.db"
        orch = Orchestrator(db_path=db)
        try:
            orch.enqueue(
                "task-3",
                "parcels/task-3.md",
                verdict_adapter="json-result",
            )
            backend = FakeBackend()
            daemon = Daemon(orch, backend, default_verdict_adapter="marker-file")
            adapter = daemon._get_verdict_adapter_for_job("task-3")
            assert isinstance(adapter, JsonResultAdapter)
        finally:
            orch.close()

    def test_daemon_falls_back_to_default_when_no_override(self, tmp_path: Path) -> None:
        """Daemon._get_verdict_adapter_for_job uses default when job has no override."""
        from claude_fleet.orchestrator.daemon import Daemon
        from claude_fleet.orchestrator.orchestrator import Orchestrator
        from tests.unit.orchestrator.fakes import FakeBackend

        db = tmp_path / "q.db"
        orch = Orchestrator(db_path=db)
        try:
            orch.enqueue("task-4", "parcels/task-4.md")
            backend = FakeBackend()
            daemon = Daemon(orch, backend, default_verdict_adapter="marker-file")
            adapter = daemon._get_verdict_adapter_for_job("task-4")
            assert isinstance(adapter, MarkerFileAdapter)
        finally:
            orch.close()

    def test_daemon_raises_for_unknown_override(self, tmp_path: Path) -> None:
        """_get_verdict_adapter_for_job raises UnknownVerdictAdapter for bad name."""
        from claude_fleet.orchestrator.daemon import Daemon
        from claude_fleet.orchestrator.orchestrator import Orchestrator
        from tests.unit.orchestrator.fakes import FakeBackend

        db = tmp_path / "q.db"
        orch = Orchestrator(db_path=db)
        try:
            orch.enqueue(
                "task-5",
                "parcels/task-5.md",
                verdict_adapter="nonexistent-adapter",
            )
            backend = FakeBackend()
            daemon = Daemon(orch, backend, default_verdict_adapter="marker-file")
            with pytest.raises(UnknownVerdictAdapter):
                daemon._get_verdict_adapter_for_job("task-5")
        finally:
            orch.close()


# ---------------------------------------------------------------------------
# VerdictResult model
# ---------------------------------------------------------------------------


class TestVerdictResult:
    def test_minimal_construction(self) -> None:
        r = VerdictResult(verdict="PASS")
        assert r.verdict == "PASS"
        assert r.summary is None
        assert r.detail == {}

    def test_full_construction(self) -> None:
        r = VerdictResult(
            verdict="BLOCK",
            summary="Tests failed",
            detail={"exit_code": 1, "failures": ["test_foo"]},
        )
        assert r.verdict == "BLOCK"
        assert r.summary == "Tests failed"
        assert r.detail["exit_code"] == 1

    def test_unknown_verdict_allowed(self) -> None:
        r = VerdictResult(verdict="UNKNOWN")
        assert r.verdict == "UNKNOWN"
