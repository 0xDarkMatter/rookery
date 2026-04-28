"""Shared test doubles for the orchestrator's backend contract.

A :class:`FakeBackend` that implements :class:`OrchestratorBackend` without
ever running a real subprocess. Per-job behaviour is scripted by the test.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rookery.adapters.base import VerdictResult
from rookery.orchestrator.backend import Job, OrchestratorBackend, WorkerHandle


@dataclass
class FakeSpec:
    """Per-job scripted behaviour for :class:`FakeBackend`.

    Attributes
    ----------
    script:
        List of ticks. Each entry is one of:
        - ``"alive"`` — worker still running, no result yet
        - ``"done:<json-serializable-summary>"`` — return a done result
        - ``"fail:<error-message>"`` — return a failed result
        - ``"dead"`` — worker exited without writing anything (is_alive→False)

        Consumed in order; beyond the list, behaviour defaults to ``"alive"``.
    spawn_error:
        If truthy, :meth:`FakeBackend.spawn` raises ``RuntimeError(spawn_error)``
        every time it's called for this job. Useful for testing retry paths.
    """

    script: list[str] = field(default_factory=list)
    spawn_error: str | None = None


class FakeBackend(OrchestratorBackend):
    """Scripted backend for daemon/integration tests."""

    def __init__(
        self,
        specs: dict[str, FakeSpec] | None = None,
        *,
        default_spawn_error: str | None = None,
    ) -> None:
        self.specs: dict[str, FakeSpec] = specs or {}
        self.default_spawn_error = default_spawn_error
        self.spawn_calls: list[str] = []
        self.terminate_calls: list[str] = []
        self._positions: dict[str, int] = {}  # job_id → script index

    def spec_for(self, job_id: str) -> FakeSpec:
        return self.specs.setdefault(job_id, FakeSpec())

    def set_spec(self, job_id: str, spec: FakeSpec) -> None:
        self.specs[job_id] = spec
        self._positions[job_id] = 0

    async def spawn(self, job: Job) -> WorkerHandle:
        self.spawn_calls.append(job.id)
        spec = self.spec_for(job.id)
        err = spec.spawn_error or self.default_spawn_error
        if err:
            raise RuntimeError(err)
        self._positions.setdefault(job.id, 0)
        return WorkerHandle(
            job_id=job.id,
            worker_id=f"fake-{uuid.uuid4().hex[:6]}",
            pid=12345,
            worktree=Path(f"/tmp/{job.id}"),
            log_path=Path(f"/tmp/{job.id}.log"),
        )

    def _advance(self, job_id: str) -> str:
        spec = self.spec_for(job_id)
        idx = self._positions.get(job_id, 0)
        instr = spec.script[idx] if idx < len(spec.script) else "alive"
        self._positions[job_id] = idx + 1
        return instr

    async def is_alive(self, handle: WorkerHandle) -> bool:
        # Look at the last instruction harvest consumed. The daemon calls
        # harvest → is_alive in that order; is_alive must reflect the same
        # state harvest just observed.
        spec = self.spec_for(handle.job_id)
        idx = max(0, self._positions.get(handle.job_id, 1) - 1)
        if not spec.script:
            return True
        instr = spec.script[idx] if idx < len(spec.script) else spec.script[-1]
        # v0.3: 'fail*' instructions mean "worker died before reporting" — same
        # liveness signal as 'dead'.  Both must report alive=False so the daemon's
        # retry path fires.
        return instr != "dead" and not instr.startswith("fail")

    async def harvest(
        self,
        handle: WorkerHandle,
        adapter: object = None,  # noqa: ARG002 — accepted for ABC parity, ignored
    ) -> VerdictResult | None:
        """Return a typed verdict per the script.

        v0.3 contract — the daemon's retry signal is (alive=False, harvest=None).
        FakeBackend script tokens:

          alive          — worker still running (harvest=None, alive=True)
          dead           — worker died without reporting; pairs with is_alive=False
          fail[:msg]     — worker errored before reporting; same as 'dead' (retry trigger)
          done[:summary] — worker reported PASS verdict
          block[:summary]— worker reported BLOCK verdict (transitions to done)
        """
        instr = self._advance(handle.job_id)
        if instr in {"alive", "dead"} or instr.startswith("fail"):
            # 'fail' replaces v0.2's "{status: failed}" path — same retry semantic
            return None
        if instr.startswith("done"):
            _, _, summary = instr.partition(":")
            return VerdictResult(verdict="PASS", summary=summary or "ok")
        if instr.startswith("block"):
            _, _, summary = instr.partition(":")
            return VerdictResult(verdict="BLOCK", summary=summary or "blocked")
        raise AssertionError(f"unknown FakeBackend script instruction: {instr!r}")

    async def terminate(self, handle: WorkerHandle) -> None:
        self.terminate_calls.append(handle.job_id)


def script_done_after(ticks: int, summary: str = "ok") -> list[str]:
    """Helper: `["alive"] * ticks + ["done:summary"]`."""
    return ["alive"] * ticks + [f"done:{summary}"]


def script_fail_after(ticks: int, error: str = "boom") -> list[str]:
    return ["alive"] * ticks + [f"fail:{error}"]


__all__ = [
    "FakeBackend",
    "FakeSpec",
    "script_done_after",
    "script_fail_after",
]


# For ``Callable`` type import compat in newer python versions
_ = Callable
