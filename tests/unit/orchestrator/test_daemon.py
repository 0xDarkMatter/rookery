"""Daemon loop tests using :class:`FakeBackend`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rookery.orchestrator import Orchestrator
from rookery.orchestrator.daemon import Daemon
from tests.unit.orchestrator.fakes import FakeBackend, FakeSpec


async def _run_for_ticks(daemon: Daemon, ticks: int) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    try:
        # Each tick is tick_interval_s apart; sleep for ticks+slack intervals.
        await asyncio.sleep(daemon.tick_interval_s * (ticks + 0.5))
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


@pytest.fixture()
async def rig(tmp_path: Path) -> AsyncIterator[tuple[Orchestrator, FakeBackend, Daemon]]:
    db = tmp_path / "daemon.db"
    orch = Orchestrator(db, lease_ttl_s=600)
    backend = FakeBackend()
    daemon = Daemon(
        orch,
        backend,
        tick_interval_s=0.05,
        max_concurrent=4,
    )
    try:
        yield orch, backend, daemon
    finally:
        orch.close()


async def test_single_job_runs_to_done(
    rig: tuple[Orchestrator, FakeBackend, Daemon],
) -> None:
    orch, backend, daemon = rig
    backend.set_spec("A", FakeSpec(script=["alive", "done:hello"]))
    orch.enqueue("A", "/tmp/a.md")

    await _run_for_ticks(daemon, ticks=4)

    final = orch.status("A")
    assert final.status == "done"
    assert final.result is not None
    # v0.3: result is now a VerdictResult.model_dump() with structured fields.
    assert final.result["verdict"] == "PASS"
    assert final.result["summary"] == "hello"
    assert backend.spawn_calls == ["A"]


async def test_dead_worker_without_result_is_failed(
    rig: tuple[Orchestrator, FakeBackend, Daemon],
) -> None:
    orch, backend, daemon = rig
    backend.set_spec("A", FakeSpec(script=["dead"]))
    orch.enqueue("A", "/tmp/a.md", max_attempts=1)

    await _run_for_ticks(daemon, ticks=4)

    final = orch.status("A")
    assert final.status == "blocked"
    assert final.last_error is not None
    # v0.3: error message switched from "PARCEL_DONE-<id>.md" filename to
    # the protocol-agnostic "without reporting a verdict".
    assert "without reporting a verdict" in final.last_error


async def test_failed_result_retries_then_blocks(
    rig: tuple[Orchestrator, FakeBackend, Daemon],
) -> None:
    orch, backend, daemon = rig
    # Every spawn yields a fail result immediately.
    backend.set_spec("A", FakeSpec(script=["fail:transient"] * 10))
    orch.enqueue("A", "/tmp/a.md", max_attempts=3)

    await _run_for_ticks(daemon, ticks=15)

    final = orch.status("A")
    assert final.status == "blocked"
    assert final.attempts == 3


async def test_multiple_jobs_run_in_parallel_up_to_cap(
    rig: tuple[Orchestrator, FakeBackend, Daemon],
) -> None:
    orch, backend, daemon = rig
    for i in range(6):
        jid = f"J{i}"
        backend.set_spec(jid, FakeSpec(script=["alive", "alive", "done:ok"]))
        orch.enqueue(jid, f"/tmp/{jid}.md")

    await _run_for_ticks(daemon, ticks=12)

    summary = orch.summary()
    assert summary["done"] == 6


async def test_spawn_failure_marks_job_failed_then_retries(
    rig: tuple[Orchestrator, FakeBackend, Daemon],
) -> None:
    orch, backend, daemon = rig
    # Every spawn attempt raises; with max_attempts=2, job blocks after 2 tries.
    backend.set_spec("A", FakeSpec(spawn_error="boom"))
    orch.enqueue("A", "/tmp/a.md", max_attempts=2)

    await _run_for_ticks(daemon, ticks=8)

    final = orch.status("A")
    assert final.status == "blocked"
    assert final.last_error is not None
    assert "boom" in final.last_error


async def test_stop_event_drains_pending_handles_to_pending(
    rig: tuple[Orchestrator, FakeBackend, Daemon],
) -> None:
    orch, backend, daemon = rig
    # Always alive — nothing finishes on its own
    backend.set_spec("A", FakeSpec(script=["alive"] * 100))
    orch.enqueue("A", "/tmp/a.md")

    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    # Let spawn happen, then stop.
    await asyncio.sleep(daemon.tick_interval_s * 3)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)

    # The job was interrupted — back to pending with attempts=0 (requeue resets).
    assert "A" in backend.terminate_calls
    job = orch.status("A")
    assert job.status == "pending"
    assert job.attempts == 0


async def test_spawn_failure_with_unloggable_exception_does_not_kill_daemon(
    rig: tuple[Orchestrator, FakeBackend, Daemon],
) -> None:
    """Regression: a UnicodeEncodeError inside ``log.exception`` must not
    propagate out of the daemon's tick loop. Previously the Windows cp1252
    console killed the daemon when subprocess error messages contained
    non-ASCII bytes (smart quotes, emoji, box-drawing). The daemon would die
    silently, leaving claimed jobs with future-dated leases that nothing
    could complete — the ghost-claim loop. Fix lives in
    ``daemon._safe_log_exception``.
    """
    orch, backend, daemon = rig
    # Exception payload contains a high code-point char that cp1252 cannot
    # encode. The daemon's logging path must absorb the encode failure.
    backend.set_spec("A", FakeSpec(spawn_error="boom — launch died"))
    orch.enqueue("A", "/tmp/a.md", max_attempts=1)

    await _run_for_ticks(daemon, ticks=4)

    final = orch.status("A")
    # The spawn failure must still be recorded — the safety net is around
    # the *logging*, not the failure handling.
    assert final.status == "blocked"
    assert final.last_error is not None
    assert "boom" in final.last_error
    # And the daemon must have stayed alive long enough to harvest the failure.
    assert backend.spawn_calls == ["A"]


async def test_safe_log_exception_swallows_unicode_encode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct unit test of the safety wrapper: simulate a structlog writer
    that raises UnicodeEncodeError, confirm the call returns cleanly."""
    from rookery.orchestrator import daemon as daemon_mod

    def boom(*args: object, **kwargs: object) -> None:
        raise UnicodeEncodeError("charmap", "x", 0, 1, "test")

    monkeypatch.setattr(daemon_mod.log, "exception", boom)

    # Must not raise.
    daemon_mod._safe_log_exception("test.event", job_id="X", err="boom")


async def test_summary_ticks_emitted_on_change(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    orch = Orchestrator(tmp_path / "e.db", lease_ttl_s=60, journal_emit=events.append)
    try:
        backend = FakeBackend()
        backend.set_spec("A", FakeSpec(script=["done:ok"]))
        orch.enqueue("A", "/tmp/a.md")
        daemon = Daemon(orch, backend, tick_interval_s=0.05, max_concurrent=2)
        await _run_for_ticks(daemon, ticks=4)

        tick_events = [e for e in events if e["kind"] == "queue.tick"]
        assert tick_events, "expected at least one queue.tick event"
    finally:
        orch.close()
