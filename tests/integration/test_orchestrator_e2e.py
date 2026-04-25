"""End-to-end integration tests for the orchestrator + daemon.

Uses :class:`FakeBackend` (no real claude subprocess). Exercises the full
lifecycle: enqueue → claim → spawn → harvest → mark_done (or mark_failed
→ retry → block). These tests are the verifier contract — the parcel
cannot ship without them green.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from claude_fleet.orchestrator import Orchestrator
from claude_fleet.orchestrator.daemon import Daemon
from tests.unit.orchestrator.fakes import FakeBackend, FakeSpec

pytestmark = pytest.mark.integration


async def _run_until_terminal(
    orch: Orchestrator,
    daemon: Daemon,
    job_ids: list[str],
    *,
    timeout_s: float = 10.0,
) -> None:
    """Run the daemon until every listed job reaches a terminal state or timeout."""

    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    deadline = asyncio.get_event_loop().time() + timeout_s
    terminal = {"done", "failed", "blocked"}
    try:
        while asyncio.get_event_loop().time() < deadline:
            statuses = {jid: orch.status(jid).status for jid in job_ids}
            if all(s in terminal for s in statuses.values()):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"timed out after {timeout_s}s waiting for terminal states; "
            f"final statuses: {statuses}"
        )
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_e2e_single_parcel(tmp_path: Path) -> None:
    """Full round-trip: enqueue → claim → spawn → done → journal."""

    events: list[dict[str, object]] = []
    orch = Orchestrator(
        tmp_path / "e2e.db", lease_ttl_s=60, journal_emit=events.append
    )
    try:
        backend = FakeBackend()
        backend.set_spec("fake-parcel", FakeSpec(script=["alive", "done:e2e"]))
        daemon = Daemon(orch, backend, tick_interval_s=0.05, max_concurrent=2)

        orch.enqueue("fake-parcel", "parcels/waves/W0/fake.md")
        await _run_until_terminal(orch, daemon, ["fake-parcel"])

        final = orch.status("fake-parcel")
        assert final.status == "done"
        assert final.result is not None
        assert final.result.get("output") == "e2e"
        assert final.completed_at is not None

        kinds = [e["kind"] for e in events]
        assert "queue.enqueue" in kinds
        assert "queue.claim" in kinds
        assert "queue.done" in kinds
        assert backend.spawn_calls == ["fake-parcel"]
    finally:
        orch.close()


async def test_e2e_dependency_order(tmp_path: Path) -> None:
    """Job B (deps=[A]) must not be claimed until A is done."""

    orch = Orchestrator(tmp_path / "e2e.db", lease_ttl_s=60)
    try:
        backend = FakeBackend()
        backend.set_spec("A", FakeSpec(script=["alive", "done:A"]))
        backend.set_spec("B", FakeSpec(script=["alive", "done:B"]))
        daemon = Daemon(orch, backend, tick_interval_s=0.05, max_concurrent=4)

        orch.enqueue("A", "/tmp/a.md")
        orch.enqueue("B", "/tmp/b.md", deps=["A"])

        await _run_until_terminal(orch, daemon, ["A", "B"])

        assert orch.status("A").status == "done"
        assert orch.status("B").status == "done"

        # A must have been spawned strictly before B.
        assert backend.spawn_calls.index("A") < backend.spawn_calls.index("B")
    finally:
        orch.close()


async def test_e2e_retry_succeeds_on_third_attempt(tmp_path: Path) -> None:
    """Two transient failures, then success. max_attempts=3 must let it finish."""

    orch = Orchestrator(tmp_path / "e2e.db", lease_ttl_s=60)
    try:
        backend = FakeBackend()
        attempts = {"count": 0}

        class RetryBackend(FakeBackend):
            async def harvest(self, handle):  # type: ignore[override]
                # First two attempts fail, third succeeds.
                attempts["count"] += 1
                if attempts["count"] <= 2:
                    return {"status": "failed", "error": "transient"}
                return {"status": "done", "output": "finally"}

        retry_backend = RetryBackend()
        daemon = Daemon(orch, retry_backend, tick_interval_s=0.05, max_concurrent=1)

        orch.enqueue("R", "/tmp/r.md", max_attempts=3)
        await _run_until_terminal(orch, daemon, ["R"], timeout_s=15.0)

        final = orch.status("R")
        assert final.status == "done"
        assert final.attempts == 3
        assert final.result is not None
        assert final.result.get("output") == "finally"
    finally:
        orch.close()


async def test_e2e_retry_blocks_after_max_attempts(tmp_path: Path) -> None:
    """Every spawn fails; with max_attempts=3, job must end ``blocked``."""

    events: list[dict[str, object]] = []
    orch = Orchestrator(
        tmp_path / "e2e.db", lease_ttl_s=60, journal_emit=events.append
    )
    try:
        backend = FakeBackend(default_spawn_error="worker boom")
        daemon = Daemon(orch, backend, tick_interval_s=0.05, max_concurrent=1)

        orch.enqueue("B", "/tmp/b.md", max_attempts=3)
        await _run_until_terminal(orch, daemon, ["B"], timeout_s=15.0)

        final = orch.status("B")
        assert final.status == "blocked"
        assert final.attempts == 3
        assert final.last_error is not None
        assert "boom" in final.last_error

        block_events = [e for e in events if e["kind"] == "queue.block"]
        assert block_events
        assert block_events[-1]["reason"] == "max-attempts"
    finally:
        orch.close()
