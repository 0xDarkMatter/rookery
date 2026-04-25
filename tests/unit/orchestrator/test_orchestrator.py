"""Tests for the Orchestrator write/read API."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_fleet.orchestrator import Job, Orchestrator
from claude_fleet.orchestrator.orchestrator import JobNotFound


@pytest.fixture()
def orch(tmp_path: Path) -> Orchestrator:
    db = tmp_path / "orch.db"
    o = Orchestrator(db)
    yield o
    o.close()


def test_enqueue_inserts_pending_job(orch: Orchestrator) -> None:
    job = orch.enqueue("W1-codex", "parcels/waves/wave-1/W1-codex.md", deps=["W0-boot"])

    assert isinstance(job, Job)
    assert job.id == "W1-codex"
    assert job.status == "pending"
    assert job.deps == ["W0-boot"]
    assert job.attempts == 0
    assert job.enqueued_at is not None


def test_enqueue_duplicate_raises(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    with pytest.raises(sqlite3.IntegrityError):
        orch.enqueue("A", "/tmp/a-2")


def test_status_unknown_raises(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.status("missing")


def test_list_jobs_sorted_by_priority_then_age(orch: Orchestrator) -> None:
    orch.enqueue("low", "/tmp/low", priority=0)
    orch.enqueue("high", "/tmp/high", priority=5)
    orch.enqueue("mid", "/tmp/mid", priority=3)

    jobs = orch.list_jobs()
    assert [j.id for j in jobs] == ["high", "mid", "low"]


def test_list_jobs_status_filter(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    orch.enqueue("B", "/tmp/b")
    orch.cancel("B")

    assert [j.id for j in orch.list_jobs(status="pending")] == ["A"]
    assert [j.id for j in orch.list_jobs(status="failed")] == ["B"]


def test_list_jobs_accepts_status_list(orch: Orchestrator) -> None:
    """list_jobs supports IN-filter via a list of statuses (used by CLI
    pseudo-statuses 'active' and 'completed')."""
    orch.enqueue("A", "/tmp/a")  # pending
    orch.enqueue("B", "/tmp/b")
    orch.cancel("B")  # failed

    active = orch.list_jobs(status=["pending", "claimed", "running", "blocked"])
    completed = orch.list_jobs(status=["done", "failed"])

    assert [j.id for j in active] == ["A"]
    assert [j.id for j in completed] == ["B"]
    # empty list short-circuits
    assert orch.list_jobs(status=[]) == []


def test_summary_has_all_statuses(orch: Orchestrator) -> None:
    s = orch.summary()
    assert set(s.keys()) == {
        "pending",
        "claimed",
        "running",
        "done",
        "failed",
        "blocked",
        "auditing",
        "audited",
        "fixing",
        "landing",
        "landed",
        "merge-blocked",
    }
    assert all(v == 0 for v in s.values())

    orch.enqueue("A", "/tmp/a")
    orch.enqueue("B", "/tmp/b")
    orch.cancel("B")

    s = orch.summary()
    assert s["pending"] == 1
    assert s["failed"] == 1


def test_cancel_marks_failed_with_error(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    job = orch.cancel("A")
    assert job.status == "failed"
    assert job.last_error == "cancelled"
    assert job.completed_at is not None


def test_cancel_unknown_raises(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.cancel("missing")


def test_cancel_is_idempotent_on_terminal(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    first = orch.cancel("A")
    second = orch.cancel("A")
    assert first.status == "failed"
    assert second.status == "failed"


def test_requeue_resets_state(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    orch.cancel("A")
    job = orch.requeue("A")
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.last_error is None


def test_requeue_unknown_raises(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.requeue("missing")


def test_journal_emit_receives_enqueue_event(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    orch = Orchestrator(tmp_path / "j.db", journal_emit=events.append)
    try:
        orch.enqueue("A", "/tmp/a", deps=["B"], priority=7)
    finally:
        orch.close()

    kinds = [e["kind"] for e in events]
    assert "queue.enqueue" in kinds
    enq = next(e for e in events if e["kind"] == "queue.enqueue")
    assert enq["job_id"] == "A"
    assert enq["deps"] == ["B"]
    assert enq["priority"] == 7


def test_journal_emit_failure_is_swallowed(tmp_path: Path) -> None:
    """A broken journal sink must never break a queue op."""

    def bad_sink(_: dict[str, object]) -> None:
        raise RuntimeError("journal down")

    orch = Orchestrator(tmp_path / "j.db", journal_emit=bad_sink)
    try:
        job = orch.enqueue("A", "/tmp/a")
        assert job.id == "A"
    finally:
        orch.close()
