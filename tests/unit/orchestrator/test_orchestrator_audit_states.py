"""Tests for the audit-loop state machine on :class:`Orchestrator`.

Covers the new columns + transitions introduced by migration 0003:

- ``verification_enabled`` defaults to True, can be disabled per-job
- ``transition()`` enforces the allowed state-machine edges
- ``attach_audit_result()`` creates ``audit_reports`` rows + updates
  the projected ``audit_iter`` / ``audit_verdict`` on ``jobs``
- ``audit_history()`` returns every iteration in ascending order
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_fleet.orchestrator import MAX_AUDIT_ITER, AuditReport, Orchestrator
from claude_fleet.orchestrator.config import AuditLoopConfig
from claude_fleet.orchestrator.orchestrator import JobNotFound


@pytest.fixture()
def orch(tmp_path: Path) -> Iterator[Orchestrator]:
    o = Orchestrator(tmp_path / "audit.db")
    try:
        yield o
    finally:
        o.close()


# --- enqueue defaults ---------------------------------------------------------


def test_enqueue_defaults_verification_enabled(orch: Orchestrator) -> None:
    job = orch.enqueue("A", "/tmp/a.md")
    assert job.verification_enabled is True
    assert job.audit_iter == 0
    assert job.audit_verdict is None
    assert job.parent_job_id is None


def test_enqueue_can_opt_out_of_verification(orch: Orchestrator) -> None:
    job = orch.enqueue("A", "/tmp/a.md", verification_enabled=False)
    assert job.verification_enabled is False


def test_enqueue_accepts_parent_job_id(orch: Orchestrator) -> None:
    orch.enqueue("parent", "/tmp/parent.md")
    child = orch.enqueue("child", "/tmp/child.md", parent_job_id="parent")
    assert child.parent_job_id == "parent"


# --- transition ---------------------------------------------------------------


def _force_status(orch: Orchestrator, job_id: str, status: str) -> None:
    """Test helper — bypass the state machine to stage a job into a status."""
    with orch._conn_lock:  # noqa: SLF001 — intentional access for fixture
        orch._conn.execute(  # noqa: SLF001
            "UPDATE jobs SET status=? WHERE id=?", (status, job_id)
        )


def test_transition_running_to_auditing(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "running")

    job = orch.transition("A", "auditing")
    assert job.status == "auditing"


def test_transition_auditing_to_audited(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "auditing")
    job = orch.transition("A", "audited")
    assert job.status == "audited"


def test_transition_audited_to_done_stamps_completed(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "audited")
    job = orch.transition("A", "done")
    assert job.status == "done"
    assert job.completed_at is not None


def test_transition_audited_to_blocked_stamps_completed(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "audited")
    job = orch.transition("A", "blocked")
    assert job.status == "blocked"
    assert job.completed_at is not None


def test_transition_audited_to_fixing(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "audited")
    job = orch.transition("A", "fixing")
    assert job.status == "fixing"


def test_transition_fixing_to_auditing(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "fixing")
    job = orch.transition("A", "auditing")
    assert job.status == "auditing"


def test_transition_rejects_illegal_edge(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "running")
    # running can only go to auditing via transition(); mark_done handles
    # running → done and the method must refuse.
    with pytest.raises(ValueError, match="illegal transition"):
        orch.transition("A", "done")


def test_transition_rejects_from_pending(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    with pytest.raises(ValueError, match="illegal transition"):
        orch.transition("A", "auditing")


def test_transition_unknown_job_raises(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.transition("missing", "auditing")


def test_transition_emits_journal(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    o = Orchestrator(tmp_path / "j.db", journal_emit=events.append)
    try:
        o.enqueue("A", "/tmp/a.md")
        _force_status(o, "A", "running")
        o.transition("A", "auditing", worker_id="daemon-1")
    finally:
        o.close()

    kinds = [e["kind"] for e in events]
    assert "queue.transition" in kinds
    tr = next(e for e in events if e["kind"] == "queue.transition")
    assert tr["from"] == "running"
    assert tr["to"] == "auditing"


# --- attach_audit_result ------------------------------------------------------


def test_attach_audit_result_writes_row_and_updates_job(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    _force_status(orch, "A", "auditing")

    job = orch.attach_audit_result(
        "A", 1, "PASS", Path("/tmp/audits/A.v1.md")
    )
    assert job.audit_iter == 1
    assert job.audit_verdict == "PASS"

    history = orch.audit_history("A")
    assert len(history) == 1
    assert history[0].verdict == "PASS"
    assert history[0].report_path == Path("/tmp/audits/A.v1.md")


def test_attach_audit_result_monotonic_iters(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    orch.attach_audit_result("A", 1, "BLOCK", Path("/tmp/A.v1.md"))
    orch.attach_audit_result("A", 2, "PASS_WITH_WARNINGS", Path("/tmp/A.v2.md"))
    orch.attach_audit_result("A", 3, "PASS", Path("/tmp/A.v3.md"))

    job = orch.status("A")
    assert job.audit_iter == 3
    assert job.audit_verdict == "PASS"

    history = orch.audit_history("A")
    assert [r.iter for r in history] == [1, 2, 3]
    assert [r.verdict for r in history] == ["BLOCK", "PASS_WITH_WARNINGS", "PASS"]


def test_attach_audit_result_rejects_duplicate_iter(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    orch.attach_audit_result("A", 1, "PASS", Path("/tmp/A.v1.md"))
    with pytest.raises(sqlite3.IntegrityError):
        orch.attach_audit_result("A", 1, "PASS", Path("/tmp/A.v1.md"))


def test_attach_audit_result_rejects_bad_verdict(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    with pytest.raises(ValueError, match="unknown verdict"):
        orch.attach_audit_result("A", 1, "GOOD", Path("/tmp/x"))  # type: ignore[arg-type]


def test_attach_audit_result_rejects_iter_below_one(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    with pytest.raises(ValueError, match="iter must be"):
        orch.attach_audit_result("A", 0, "PASS", Path("/tmp/x"))


def test_attach_audit_result_unknown_job_raises(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.attach_audit_result("missing", 1, "PASS", Path("/tmp/x"))


def test_attach_audit_result_emits_journal(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    o = Orchestrator(tmp_path / "j.db", journal_emit=events.append)
    try:
        o.enqueue("A", "/tmp/a.md")
        o.attach_audit_result("A", 1, "BLOCK", Path("/tmp/A.v1.md"))
    finally:
        o.close()
    kinds = [e["kind"] for e in events]
    assert "queue.audit" in kinds


def test_audit_history_empty_for_no_audits(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    assert orch.audit_history("A") == []


def test_audit_history_returns_audit_report_models(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a.md")
    orch.attach_audit_result("A", 1, "PASS", Path("/tmp/A.v1.md"))
    history = orch.audit_history("A")
    assert all(isinstance(r, AuditReport) for r in history)


def test_audit_history_unknown_job_raises(orch: Orchestrator) -> None:
    with pytest.raises(JobNotFound):
        orch.audit_history("missing")


# --- max-iter constant -------------------------------------------------------


def test_max_audit_iter_matches_shell_script() -> None:
    # Guardrail: the daemon's hardcoded MAX_AUDIT_ITER and the
    # AuditLoopConfig.max_iter default live in separate modules; keep
    # them in lockstep so a daemon-side audit tracker and the CLI-side
    # audit-loop agree on when to give up.
    assert MAX_AUDIT_ITER == AuditLoopConfig().max_iter == 3


# --- backward compat ---------------------------------------------------------


def test_verification_disabled_job_stays_in_legacy_lifecycle(
    orch: Orchestrator,
) -> None:
    """A job enqueued with verification_enabled=False must still be claimable
    and completable via mark_done; nothing about the audit columns should
    block the existing ``running → done`` path."""

    orch.enqueue("legacy", "/tmp/legacy.md", verification_enabled=False)
    claimed = orch.claim_next("w1")
    assert claimed is not None and claimed.id == "legacy"

    orch.mark_done("legacy", {"ok": True}, worker_id="w1")
    final = orch.status("legacy")
    assert final.status == "done"
    assert final.audit_iter == 0
    assert final.audit_verdict is None
