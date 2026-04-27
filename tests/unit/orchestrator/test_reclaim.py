"""Tests for lease reclaim semantics.

A worker is expected to heartbeat on a regular cadence. If its lease expires
(crash, hang, OS reboot), the daemon's next reclaim_expired() call must
either return the job to pending (if retries remain) or flip it to blocked
(if attempts >= max_attempts).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rookery.orchestrator import Orchestrator


def _force_expired_lease(orch: Orchestrator, job_id: str) -> None:
    """Directly shove lease_expires into the past for test purposes."""
    orch._conn.execute(  # noqa: SLF001
        "UPDATE jobs SET lease_expires = '1970-01-01T00:00:00+00:00' WHERE id = ?",
        (job_id,),
    )


@pytest.fixture()
def orch(tmp_path: Path) -> Orchestrator:
    db = tmp_path / "reclaim.db"
    o = Orchestrator(db, lease_ttl_s=60)
    yield o
    o.close()


def test_reclaim_returns_nothing_when_no_expired(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    orch.claim_next("w1")
    # Lease is 60s in the future — nothing to reclaim.
    assert orch.reclaim_expired() == []


def test_expired_lease_with_retries_flips_to_pending(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a", max_attempts=3)
    claimed = orch.claim_next("w1")
    assert claimed is not None
    assert claimed.attempts == 1

    _force_expired_lease(orch, "A")
    reclaimed = orch.reclaim_expired()
    assert reclaimed == ["A"]

    after = orch.status("A")
    assert after.status == "pending"
    assert after.claimed_by is None
    assert after.lease_expires is None
    assert after.attempts == 1  # counter preserved across reclaim

    # Next claim should pick it up and bump attempts.
    next_claim = orch.claim_next("w2")
    assert next_claim is not None
    assert next_claim.attempts == 2


def test_expired_lease_past_max_attempts_flips_to_blocked(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a", max_attempts=2)

    # First expiry — still under cap
    orch.claim_next("w1")
    _force_expired_lease(orch, "A")
    orch.reclaim_expired()
    assert orch.status("A").status == "pending"

    # Second expiry — cap hit, flip to blocked
    orch.claim_next("w2")
    _force_expired_lease(orch, "A")
    reclaimed = orch.reclaim_expired()
    assert reclaimed == ["A"]

    final = orch.status("A")
    assert final.status == "blocked"
    assert final.last_error == "lease-expired"
    assert final.attempts == 2


def test_heartbeat_extends_lease_and_marks_running(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    claimed = orch.claim_next("w1")
    assert claimed is not None

    # Force the lease into the past, then heartbeat — reclaim should see nothing.
    _force_expired_lease(orch, "A")
    orch.heartbeat("A", worker_id="w1")
    assert orch.reclaim_expired() == []

    after = orch.status("A")
    assert after.status == "running"
    assert after.lease_expires is not None


def test_heartbeat_for_wrong_worker_is_ignored(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    orch.claim_next("w1")
    _force_expired_lease(orch, "A")
    orch.heartbeat("A", worker_id="stranger")

    # Heartbeat had no effect — reclaim still flips it.
    reclaimed = orch.reclaim_expired()
    assert reclaimed == ["A"]


def test_mark_done_finalises_job(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    orch.claim_next("w1")
    orch.mark_done("A", {"status": "done", "output": "ok"}, worker_id="w1")

    job = orch.status("A")
    assert job.status == "done"
    assert job.completed_at is not None
    assert job.result == {"status": "done", "output": "ok"}
    assert job.lease_expires is None


def test_mark_failed_under_cap_flips_to_pending(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a", max_attempts=3)
    orch.claim_next("w1")
    orch.mark_failed("A", "boom", worker_id="w1")

    job = orch.status("A")
    assert job.status == "pending"
    assert job.last_error == "boom"


def test_mark_failed_at_cap_flips_to_blocked(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a", max_attempts=1)
    orch.claim_next("w1")  # attempts=1 == cap
    orch.mark_failed("A", "boom", worker_id="w1")

    job = orch.status("A")
    assert job.status == "blocked"
    assert job.last_error == "boom"


def test_journal_emits_reclaim_and_block_events(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    orch = Orchestrator(tmp_path / "j.db", lease_ttl_s=60, journal_emit=events.append)
    try:
        orch.enqueue("A", "/tmp/a", max_attempts=2)

        # First lease expiry — reclaim event
        orch.claim_next("w1")
        _force_expired_lease(orch, "A")
        orch.reclaim_expired()

        kinds = [e["kind"] for e in events]
        assert "queue.reclaim" in kinds

        # Second lease expiry — block event
        events.clear()
        orch.claim_next("w2")
        _force_expired_lease(orch, "A")
        orch.reclaim_expired()

        kinds = [e["kind"] for e in events]
        assert "queue.block" in kinds
        block_event = next(e for e in events if e["kind"] == "queue.block")
        assert block_event["reason"] == "lease-expired"
    finally:
        orch.close()
