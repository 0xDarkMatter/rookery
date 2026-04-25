"""Tests for claim_next atomicity, dep-ordering, and retry state.

Two threads racing for the same job must produce exactly one winner —
SQLite BEGIN IMMEDIATE + 10s busy_timeout is the primitive we rely on for
cross-process safety.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from claude_fleet.orchestrator import Orchestrator


@pytest.fixture()
def orch(tmp_path: Path) -> Orchestrator:
    db = tmp_path / "orch.db"
    o = Orchestrator(db)
    yield o
    o.close()


def test_claim_next_returns_none_when_empty(orch: Orchestrator) -> None:
    assert orch.claim_next("w1") is None


def test_claim_next_happy_path(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    job = orch.claim_next("w1")
    assert job is not None
    assert job.id == "A"
    assert job.status == "claimed"
    assert job.claimed_by == "w1"
    assert job.attempts == 1
    assert job.started_at is not None
    assert job.claimed_at is not None
    assert job.lease_expires is not None


def test_claim_next_respects_priority(orch: Orchestrator) -> None:
    orch.enqueue("low", "/tmp/low", priority=0)
    orch.enqueue("high", "/tmp/high", priority=5)
    orch.enqueue("mid", "/tmp/mid", priority=3)

    assert orch.claim_next("w1").id == "high"  # type: ignore[union-attr]
    assert orch.claim_next("w1").id == "mid"  # type: ignore[union-attr]
    assert orch.claim_next("w1").id == "low"  # type: ignore[union-attr]
    assert orch.claim_next("w1") is None


def test_claim_next_respects_dependencies(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a")
    orch.enqueue("B", "/tmp/b", deps=["A"])

    # B is not ready yet — deps not done
    first = orch.claim_next("w1")
    assert first is not None
    assert first.id == "A"

    # Still not ready — A is claimed but not done
    assert orch.claim_next("w2") is None

    orch.mark_done("A", {"ok": True})
    second = orch.claim_next("w3")
    assert second is not None
    assert second.id == "B"


def test_concurrent_claims_are_mutually_exclusive(tmp_path: Path) -> None:
    """Two threads racing must together claim each job exactly once."""

    db = tmp_path / "race.db"
    orch = Orchestrator(db)
    try:
        for i in range(10):
            orch.enqueue(f"J{i}", f"/tmp/j{i}")

        results: list[str] = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            while True:
                job = orch.claim_next(name)
                if job is None:
                    return
                with lock:
                    results.append(job.id)

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive(), "worker thread stuck"

        # Every job claimed exactly once; every enqueued job accounted for.
        assert sorted(results) == sorted(f"J{i}" for i in range(10))
        assert len(results) == len(set(results))
    finally:
        orch.close()


def test_attempts_counter_increments_on_each_claim(orch: Orchestrator) -> None:
    orch.enqueue("A", "/tmp/a", max_attempts=5)

    first = orch.claim_next("w1")
    assert first is not None
    assert first.attempts == 1

    # Simulate a failed run — goes back to pending with attempts preserved.
    orch.mark_failed("A", "worker crashed", worker_id="w1")

    again = orch.status("A")
    assert again.status == "pending"
    assert again.attempts == 1  # mark_failed doesn't touch attempts
    assert again.last_error == "worker crashed"

    second = orch.claim_next("w2")
    assert second is not None
    assert second.attempts == 2
