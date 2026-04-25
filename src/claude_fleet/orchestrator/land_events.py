"""Typed outcome / handle models for :class:`LandBackend`.

Deliberately kept out of :mod:`axiom.orchestrator.backend` because the
OrchestratorBackend ABC is about build workers; land attempts are a
different concern (no worktree spawn, no claude subprocess, no heartbeat).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from axiom.orchestrator.backend import LandOutcome, LandPhase

# The subset of ``LandOutcome`` values that a full attempt can terminate on.
# ``skipped`` is a per-phase marker (e.g. fetch-skipped-on-dry-run) and never
# becomes a whole-attempt outcome; it is still accepted on
# ``record_land_event`` rows.
LandTerminalOutcome = Literal["ok", "conflict", "failed", "non-ff", "timeout"]


@dataclass(frozen=True)
class LandResult:
    """Terminal outcome of one land-attempt chain.

    ``commit`` is populated only on ``outcome == "ok"`` and holds the SHA
    that ``main`` now points at. ``detail`` is a free-text note suitable for
    the operator — conflict file list, failing test name, the non-FF main
    tip sha, or the error string when the chain crashed unexpectedly.
    """

    outcome: LandTerminalOutcome
    attempt: int
    detail: str | None = None
    commit: str | None = None


@dataclass
class LandHandle:
    """Returned from :meth:`LandBackend.spawn_land`; passed back to
    :meth:`~LandBackend.harvest` and :meth:`~LandBackend.terminate`.

    The backing :class:`asyncio.Task` drives the subprocess chain. Keeping
    it on the handle (rather than in a backend-owned dict) mirrors the
    ``WorkerHandle`` pattern used by :class:`OrchestratorBackend`.
    """

    job_id: str
    attempt: int
    task: asyncio.Task[LandResult]


@runtime_checkable
class EventSink(Protocol):
    """Sink for :class:`LandBackend` phase events.

    Implementations are expected to forward each call to
    ``Orchestrator.record_land_event``. Kept as a Protocol so tests can pass
    a plain list-appending lambda without inheriting from a dataclass.
    """

    def __call__(
        self,
        job_id: str,
        attempt: int,
        phase: LandPhase,
        outcome: LandOutcome,
        *,
        detail: str | None = None,
        commit_sha: str | None = None,
    ) -> None: ...


__all__ = [
    "EventSink",
    "LandHandle",
    "LandResult",
    "LandTerminalOutcome",
]
