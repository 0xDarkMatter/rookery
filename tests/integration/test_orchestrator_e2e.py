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

from rookery.orchestrator import Orchestrator
from rookery.orchestrator.daemon import Daemon
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
        # v0.3: mark_done stores VerdictResult.model_dump(mode='json'),
        # so structured fields appear at the top level.
        assert final.result.get("verdict") == "PASS"
        assert final.result.get("summary") == "e2e"
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
        attempts = {"count": 0}

        from rookery.adapters.base import VerdictResult

        class RetryBackend(FakeBackend):
            """Simulates 'worker died before reporting' for the first 2 attempts.

            The v0.3 retry signal is (alive=False, harvest=None) — i.e. the
            worker subprocess exited without producing a verdict.  Both the
            harvest and is_alive sides have to agree.
            """

            async def harvest(self, handle, adapter=None):  # type: ignore[override]
                attempts["count"] += 1
                if attempts["count"] <= 2:
                    # Worker died before reporting; daemon must retry.
                    return None
                return VerdictResult(verdict="PASS", summary="finally")

            async def is_alive(self, handle):  # type: ignore[override]
                # Always report dead — paired with harvest=None on attempts 1-2
                # this triggers mark_failed → retry; paired with a real
                # VerdictResult on attempt 3 the daemon transitions to done
                # regardless of liveness.
                return False

        retry_backend = RetryBackend()
        daemon = Daemon(orch, retry_backend, tick_interval_s=0.05, max_concurrent=1)

        orch.enqueue("R", "/tmp/r.md", max_attempts=3)
        await _run_until_terminal(orch, daemon, ["R"], timeout_s=15.0)

        final = orch.status("R")
        assert final.status == "done"
        assert final.attempts == 3
        assert final.result is not None
        # mark_done now stores the typed VerdictResult.model_dump(mode='json')
        assert final.result.get("verdict") == "PASS"
        assert final.result.get("summary") == "finally"
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
