"""Tests for the auto-land state machine on :class:`Orchestrator`.

Covers the new columns + transitions introduced by migration 0004:

- ``land_attempts`` defaults to 0 and increments on ``begin_landing``
- ``begin_landing`` / ``mark_landed`` / ``mark_merge_blocked`` enforce
  current-status and verdict preconditions
- ``record_land_event`` writes into ``land_events`` with enum validation
- ``land_history`` returns every event in insertion order
- ``transition()`` rejects the new statuses as a source so the daemon can't
  accidentally short-circuit ``landing → done``
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_fleet.orchestrator import LandEvent, Orchestrator
from claude_fleet.orchestrator.orchestrator import JobNotFound


@pytest.fixture()
def orch(tmp_path: Path) -> Iterator[Orchestrator]:
    o = Orchestrator(tmp_path / "land.db")
    try:
        yield o
    finally:
        o.close()


def _force_status(orch: Orchestrator, job_id: str, status: str) -> None:
    with orch._conn_lock:  # noqa: SLF001 — intentional access for fixture
        orch._conn.execute(  # noqa: SLF001
            "UPDATE jobs SET status=? WHERE id=?", (status, job_id)
        )


def _set_verdict(orch: Orchestrator, job_id: str, verdict: str) -> None:
    with orch._conn_lock:  # noqa: SLF001 — intentional access for fixture
        orch._conn.execute(  # noqa: SLF001
            "UPDATE jobs SET audit_verdict=? WHERE id=?", (verdict, job_id)
        )


# --- defaults ----------------------------------------------------------------


def test_enqueue_defaults_land_columns(orch: Orchestrator) -> None:
    job = orch.enqueue("A", "/tmp/a.md")
    assert job.land_attempts == 0
    assert job.landed_commit is None
    assert job.merge_block_reason is None


# --- begin_landing -----------------------------------------------------------


def test_begin_landing_requires_audited_and_pass(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "audited")
    _set_verdict(orch, "A", "PASS")

    job = orch.begin_landing("A")
    assert job.status == "landing"
    assert job.land_attempts == 1


def test_begin_landing_rejects_non_audited(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "running")
    _set_verdict(orch, "A", "PASS")

    with pytest.raises(ValueError, match="status='audited'"):
        orch.begin_landing("A")


def test_begin_landing_rejects_non_pass_verdict(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "audited")
    _set_verdict(orch, "A", "BLOCK")

    with pytest.raises(ValueError, match="verdict='PASS'"):
        orch.begin_landing("A")


def test_begin_landing_increments_attempts(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "audited")
    _set_verdict(orch, "A", "PASS")
    orch.begin_landing("A")

    # Simulate a retry after a merge-block: operator re-audits + PASS, then
    # begin_landing again.
    _force_status(orch, "A", "audited")
    job = orch.begin_landing("A")
    assert job.land_attempts == 2


def test_begin_landing_unknown_job_raises(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.begin_landing("missing")


def test_begin_landing_emits_journal(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    o = Orchestrator(tmp_path / "j.db", journal_emit=events.append)
    try:
        o.enqueue("A", "/tmp/a.md")
        _force_status(o, "A", "audited")
        _set_verdict(o, "A", "PASS")
        o.begin_landing("A")
    finally:
        o.close()

    kinds = [e["kind"] for e in events]
    assert "queue.land.start" in kinds


# --- mark_landed -------------------------------------------------------------


def test_mark_landed_requires_landing(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "landing")

    job = orch.mark_landed("A", "abc123")
    assert job.status == "landed"
    assert job.landed_commit == "abc123"
    assert job.completed_at is not None


def test_mark_landed_rejects_non_landing(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "audited")

    with pytest.raises(ValueError, match="status='landing'"):
        orch.mark_landed("A", "abc123")


def test_mark_landed_requires_commit(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "landing")

    with pytest.raises(ValueError, match="non-empty commit"):
        orch.mark_landed("A", "")


def test_mark_landed_unknown_job_raises(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.mark_landed("missing", "abc")


def test_mark_landed_emits_journal(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    o = Orchestrator(tmp_path / "j.db", journal_emit=events.append)
    try:
        o.enqueue("A", "/tmp/a.md")
        _force_status(o, "A", "landing")
        o.mark_landed("A", "abc123")
    finally:
        o.close()
    kinds = [e["kind"] for e in events]
    assert "queue.land.done" in kinds


# --- mark_merge_blocked ------------------------------------------------------


def test_mark_merge_blocked_requires_landing(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "landing")

    job = orch.mark_merge_blocked("A", "rebase-conflict", detail="src/foo.py")
    assert job.status == "merge-blocked"
    assert job.merge_block_reason == "rebase-conflict"
    assert job.last_error == "src/foo.py"
    assert job.completed_at is not None


def test_mark_merge_blocked_rejects_non_landing(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "audited")

    with pytest.raises(ValueError, match="status='landing'"):
        orch.mark_merge_blocked("A", "rebase-conflict")


def test_mark_merge_blocked_rejects_unknown_reason(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "landing")

    with pytest.raises(ValueError, match="unknown merge_block reason"):
        orch.mark_merge_blocked("A", "not-a-reason")  # type: ignore[arg-type]


def test_mark_merge_blocked_accepts_all_enumerated_reasons(
    orch: Orchestrator,
) -> None:
    for reason in (
        "rebase-conflict",
        "tests-failed",
        "non-ff",
        "timeout",
        "other",
    ):
        jid = f"J_{reason}"
        orch.enqueue(jid, f"/tmp/{jid}.md")
        _force_status(orch, jid, "landing")
        job = orch.mark_merge_blocked(jid, reason)  # type: ignore[arg-type]
        assert job.merge_block_reason == reason


def test_mark_merge_blocked_emits_journal(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    o = Orchestrator(tmp_path / "j.db", journal_emit=events.append)
    try:
        o.enqueue("A", "/tmp/a.md")
        _force_status(o, "A", "landing")
        o.mark_merge_blocked("A", "tests-failed", detail="test_foo")
    finally:
        o.close()
    kinds = [e["kind"] for e in events]
    assert "queue.land.blocked" in kinds


# --- record_land_event / land_history ---------------------------------------


def test_record_land_event_writes_row(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    ev = orch.record_land_event("A", 1, "start", "ok")
    assert isinstance(ev, LandEvent)
    assert ev.phase == "start"
    assert ev.outcome == "ok"


def test_record_land_event_stores_commit_sha(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    ev = orch.record_land_event(
        "A", 1, "ff", "ok", commit_sha="deadbeef"
    )
    assert ev.commit_sha == "deadbeef"


def test_record_land_event_rejects_bad_phase(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    with pytest.raises(ValueError, match="unknown land phase"):
        orch.record_land_event("A", 1, "tacos", "ok")  # type: ignore[arg-type]


def test_record_land_event_rejects_bad_outcome(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    with pytest.raises(ValueError, match="unknown land outcome"):
        orch.record_land_event("A", 1, "start", "bonked")  # type: ignore[arg-type]


def test_record_land_event_rejects_attempt_zero(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    with pytest.raises(ValueError, match="attempt must be"):
        orch.record_land_event("A", 0, "start", "ok")


def test_record_land_event_unknown_job(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.record_land_event("missing", 1, "start", "ok")


def test_land_history_preserves_insertion_order(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    orch.record_land_event("A", 1, "start", "ok")
    orch.record_land_event("A", 1, "rebase", "ok")
    orch.record_land_event("A", 1, "tests", "failed", detail="test_x")

    history = orch.land_history("A")
    assert [e.phase for e in history] == ["start", "rebase", "tests"]
    assert [e.outcome for e in history] == ["ok", "ok", "failed"]
    assert history[-1].detail == "test_x"


def test_land_history_empty_for_no_attempts(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    assert orch.land_history("A") == []


def test_land_history_unknown_job_raises(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.land_history("missing")


# --- forbidden transitions ---------------------------------------------------


def test_transition_rejects_landing_to_done_direct(orch: Orchestrator) -> None:
    """The daemon must go through ``mark_landed`` / ``mark_merge_blocked``;
    bypassing them via ``transition()`` must fail."""
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "landing")

    with pytest.raises(ValueError, match="illegal transition"):
        orch.transition("A", "done")


def test_transition_rejects_audited_to_landing(orch: Orchestrator) -> None:
    """``audited → landing`` is owned by :meth:`begin_landing`; plain
    ``transition()`` must refuse it so the verdict/attempts invariants are
    not bypassed."""
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "audited")

    with pytest.raises(ValueError, match="illegal transition"):
        orch.transition("A", "landing")


def test_transition_rejects_landing_source(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "landing")

    with pytest.raises(ValueError, match="illegal transition"):
        orch.transition("A", "auditing")
