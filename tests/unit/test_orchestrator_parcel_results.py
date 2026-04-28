"""Unit tests for ``Orchestrator.write_parcel_result`` / ``append_parcel_event``.

Phase 0 of the v0.3 parcel-reporting refactor: structured verdict + streaming
events tables.  These tests cover the DB primitives the CLI helpers
(``rookery parcel done`` / ``rookery parcel progress``) and the new
``DbResultAdapter`` will sit on top of.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from rookery.orchestrator.orchestrator import JobNotFound, Orchestrator


@pytest.fixture
def orch(tmp_path: Path) -> Generator[Orchestrator, None, None]:
    db_path = tmp_path / "rookery.db"
    o = Orchestrator(db_path, lease_ttl_s=60)
    # Always start with a real job row so foreign-key constraints are happy.
    o.enqueue("test-parcel", "parcels/test.md")
    yield o
    o.close()


class TestWriteParcelResult:
    def test_minimal_round_trip(self, orch: Orchestrator) -> None:
        """Verdict + summary alone (no metadata) round-trip cleanly."""
        orch.write_parcel_result(
            "test-parcel",
            attempt=1,
            verdict="PASS",
            summary="all green",
            reported_via="cli",
        )
        row = orch.read_parcel_result("test-parcel", attempt=1)
        assert row is not None
        assert row["verdict"] == "PASS"
        assert row["summary"] == "all green"
        assert row["reported_via"] == "cli"
        assert row["attempt"] == 1
        # Optional metadata defaults to NULL when not provided
        assert row["tokens_in"] is None
        assert row["duration_s"] is None
        assert row["tests_passed"] is None

    def test_full_metadata_round_trip(self, orch: Orchestrator) -> None:
        """Every optional field round-trips with the right type."""
        orch.write_parcel_result(
            "test-parcel",
            attempt=1,
            verdict="PASS_WITH_WARNINGS",
            summary="OAuth flow added",
            reported_via="cli",
            detail_md="# Detail\n\nBody text.",
            tokens_in=12500,
            tokens_out=3200,
            duration_s=187.5,
            tests_passed=42,
            tests_failed=1,
            files_changed=3,
        )
        row = orch.read_parcel_result("test-parcel")
        assert row is not None
        assert row["verdict"] == "PASS_WITH_WARNINGS"
        assert row["detail_md"] == "# Detail\n\nBody text."
        assert row["tokens_in"] == 12500
        assert row["tokens_out"] == 3200
        assert row["duration_s"] == pytest.approx(187.5)
        assert row["tests_passed"] == 42
        assert row["tests_failed"] == 1
        assert row["files_changed"] == 3

    def test_invalid_verdict_rejected_at_api_layer(self, orch: Orchestrator) -> None:
        """API-level validation rejects unknown verdict tokens before SQL."""
        with pytest.raises(ValueError, match="verdict must be one of"):
            orch.write_parcel_result(
                "test-parcel",
                attempt=1,
                verdict="WHATEVER",
                summary="x",
                reported_via="cli",
            )

    def test_invalid_reported_via_rejected_at_api_layer(self, orch: Orchestrator) -> None:
        with pytest.raises(ValueError, match="reported_via must be one of"):
            orch.write_parcel_result(
                "test-parcel",
                attempt=1,
                verdict="PASS",
                summary="x",
                reported_via="invalid-source",
            )

    def test_db_check_constraint_rejects_bad_verdict(self, tmp_path: Path) -> None:
        """Defence-in-depth: even bypassing the API, the DB CHECK rejects bad tokens."""
        db_path = tmp_path / "rookery.db"
        orch = Orchestrator(db_path, lease_ttl_s=60)
        orch.enqueue("p", "x.md")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                orch._conn.execute(
                    "INSERT INTO parcel_results "
                    "(job_id, attempt, verdict, summary, reported_via) "
                    "VALUES ('p', 1, 'BOGUS', 's', 'cli')"
                )
        finally:
            orch.close()

    def test_unique_job_attempt_replaces_via_or_replace(self, orch: Orchestrator) -> None:
        """Re-writing the same (job_id, attempt) overwrites — no integrity error."""
        orch.write_parcel_result(
            "test-parcel",
            attempt=1,
            verdict="PASS",
            summary="first",
            reported_via="cli",
        )
        orch.write_parcel_result(
            "test-parcel",
            attempt=1,
            verdict="BLOCK",
            summary="changed mind",
            reported_via="cli",
        )
        row = orch.read_parcel_result("test-parcel", attempt=1)
        assert row is not None
        assert row["verdict"] == "BLOCK"
        assert row["summary"] == "changed mind"

    def test_different_attempts_coexist(self, orch: Orchestrator) -> None:
        """Two attempts → two rows; latest wins on read_parcel_result()."""
        orch.write_parcel_result(
            "test-parcel",
            attempt=1,
            verdict="BLOCK",
            summary="first try failed",
            reported_via="cli",
        )
        orch.write_parcel_result(
            "test-parcel",
            attempt=2,
            verdict="PASS",
            summary="second try worked",
            reported_via="cli",
        )
        latest = orch.read_parcel_result("test-parcel")
        assert latest is not None
        assert latest["attempt"] == 2
        assert latest["verdict"] == "PASS"

        first = orch.read_parcel_result("test-parcel", attempt=1)
        assert first is not None
        assert first["verdict"] == "BLOCK"

    def test_unknown_job_raises(self, orch: Orchestrator) -> None:
        with pytest.raises(JobNotFound):
            orch.write_parcel_result(
                "does-not-exist",
                attempt=1,
                verdict="PASS",
                summary="x",
                reported_via="cli",
            )

    def test_read_returns_none_for_missing(self, orch: Orchestrator) -> None:
        assert orch.read_parcel_result("test-parcel", attempt=99) is None
        assert orch.read_parcel_result("test-parcel") is None  # no rows yet


class TestAppendParcelEvent:
    def test_basic_append_and_read(self, orch: Orchestrator) -> None:
        eid = orch.append_parcel_event(
            "test-parcel",
            attempt=1,
            event_type="progress",
            label="Implemented OAuth",
        )
        assert eid > 0
        events = orch.read_parcel_events("test-parcel")
        assert len(events) == 1
        assert events[0]["event_type"] == "progress"
        assert events[0]["label"] == "Implemented OAuth"

    def test_events_ordered_oldest_first(self, orch: Orchestrator) -> None:
        for i in range(3):
            orch.append_parcel_event(
                "test-parcel",
                attempt=1,
                event_type="progress",
                label=f"step {i}",
            )
        events = orch.read_parcel_events("test-parcel")
        labels = [e["label"] for e in events]
        assert labels == ["step 0", "step 1", "step 2"]

    def test_since_id_returns_only_newer(self, orch: Orchestrator) -> None:
        ids = [
            orch.append_parcel_event("test-parcel", 1, "progress", label=f"s{i}")
            for i in range(4)
        ]
        # Read events strictly newer than ids[1] → expect s2 + s3
        newer = orch.read_parcel_events("test-parcel", since_id=ids[1])
        assert [e["label"] for e in newer] == ["s2", "s3"]

    def test_payload_json_serialized(self, orch: Orchestrator) -> None:
        orch.append_parcel_event(
            "test-parcel",
            attempt=1,
            event_type="progress",
            payload={"step": 3, "total": 5},
        )
        events = orch.read_parcel_events("test-parcel")
        assert events[0]["payload_json"] == '{"step": 3, "total": 5}'

    def test_attempt_filter(self, orch: Orchestrator) -> None:
        orch.append_parcel_event("test-parcel", 1, "progress", label="a1")
        orch.append_parcel_event("test-parcel", 2, "progress", label="a2")
        only_attempt1 = orch.read_parcel_events("test-parcel", attempt=1)
        assert [e["label"] for e in only_attempt1] == ["a1"]
        only_attempt2 = orch.read_parcel_events("test-parcel", attempt=2)
        assert [e["label"] for e in only_attempt2] == ["a2"]

    def test_unknown_job_raises(self, orch: Orchestrator) -> None:
        with pytest.raises(JobNotFound):
            orch.append_parcel_event(
                "does-not-exist",
                attempt=1,
                event_type="progress",
            )


class TestSchemaShape:
    """Shape-of-the-table sanity checks distinct from API behaviour."""

    def test_check_constraint_rejects_unknown_reported_via(
        self, orch: Orchestrator
    ) -> None:
        with orch._conn_lock, pytest.raises(sqlite3.IntegrityError):
            orch._conn.execute(
                "INSERT INTO parcel_results "
                "(job_id, attempt, verdict, summary, reported_via) "
                "VALUES ('test-parcel', 1, 'PASS', 's', 'unknown_source')"
            )

    def test_unique_constraint_at_db_level(self, orch: Orchestrator) -> None:
        """Belt-and-braces: API uses INSERT OR REPLACE, but a raw INSERT
        without the OR REPLACE clause must still be rejected by UNIQUE."""
        orch.write_parcel_result(
            "test-parcel",
            attempt=1,
            verdict="PASS",
            summary="first",
            reported_via="cli",
        )
        with orch._conn_lock, pytest.raises(sqlite3.IntegrityError):
            orch._conn.execute(
                "INSERT INTO parcel_results "
                "(job_id, attempt, verdict, summary, reported_via) "
                "VALUES ('test-parcel', 1, 'BLOCK', 'dup', 'cli')"
            )
