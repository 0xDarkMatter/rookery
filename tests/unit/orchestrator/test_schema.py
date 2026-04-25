"""Tests for the schema migration runner."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from claude_fleet.orchestrator import schema


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


_EXPECTED_MIGRATIONS = [
    "0001_initial.sql",
    "0003_audit_integration.sql",
    "0004_auto_land.sql",
    "0005_verdict_adapter.sql",
]


def test_apply_migrations_creates_tables_indexes_view(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    applied = schema.apply_migrations(db)

    assert applied == _EXPECTED_MIGRATIONS

    conn = schema.open_connection(db)
    try:
        tables = _table_names(conn)
        assert "jobs" in tables
        assert "job_events" in tables
        assert "jobs_ready" in tables
        assert "audit_reports" in tables
        assert "jobs_with_audit" in tables
        assert "land_events" in tables
        assert "_applied_migrations" in tables

        indexes = _index_names(conn)
        assert "idx_jobs_status" in indexes
        assert "idx_jobs_priority_enqueued" in indexes
        assert "idx_job_events_job_ts" in indexes
        assert "idx_audit_reports_job" in indexes
        assert "idx_jobs_parent" in indexes
        assert "idx_land_events_job" in indexes

        # Verify new jobs columns survived the 0004 rebuild.
        jobs_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        assert "land_attempts" in jobs_cols
        assert "landed_commit" in jobs_cols
        assert "merge_block_reason" in jobs_cols
        # G4: per-parcel verdict adapter override column (migration 0005).
        assert "verdict_adapter" in jobs_cols
    finally:
        conn.close()


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    first = schema.apply_migrations(db)
    second = schema.apply_migrations(db)

    assert first == _EXPECTED_MIGRATIONS
    assert second == []


def test_status_check_constraint_rejects_unknown(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    schema.apply_migrations(db)
    conn = schema.open_connection(db)
    try:
        try:
            conn.execute(
                "INSERT INTO jobs (id, prompt_path, status) VALUES (?, ?, ?)",
                ("bad", "/tmp/x", "not-a-status"),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("expected IntegrityError for bad status")
    finally:
        conn.close()


def test_jobs_ready_view_filters_on_deps(tmp_path: Path) -> None:
    """Jobs with pending deps must NOT appear in jobs_ready."""
    db = tmp_path / "orch.db"
    schema.apply_migrations(db)
    conn = schema.open_connection(db)
    try:
        conn.execute(
            "INSERT INTO jobs (id, prompt_path, status, deps_json) VALUES (?, ?, ?, ?)",
            ("A", "/tmp/a", "pending", "[]"),
        )
        conn.execute(
            "INSERT INTO jobs (id, prompt_path, status, deps_json) VALUES (?, ?, ?, ?)",
            ("B", "/tmp/b", "pending", '["A"]'),
        )

        ready = {row[0] for row in conn.execute("SELECT id FROM jobs_ready").fetchall()}
        assert ready == {"A"}

        conn.execute("UPDATE jobs SET status='done' WHERE id='A'")
        ready = {row[0] for row in conn.execute("SELECT id FROM jobs_ready").fetchall()}
        assert ready == {"B"}
    finally:
        conn.close()


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    schema.apply_migrations(db)
    conn = schema.open_connection(db)
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()
