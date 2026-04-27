"""Orchestrator: persistent parcel-dispatch queue.

The orchestrator is the dispatcher that decides *what* parcel to launch
and *when*. Actual worker spawning lives in
:mod:`rookery.orchestrator.worker_backend`; landing (merge to main)
lives in :mod:`rookery.orchestrator.land_backend`.

Public surface (stable for downstream consumers)::

    from rookery.orchestrator import Orchestrator, OrchestratorConfig
    from rookery.orchestrator.backend import OrchestratorBackend, Job, WorkerHandle
    from rookery.orchestrator.worker_backend import WorkerBackend

CLI (Typer) — see :mod:`rookery.cli` for the top-level ``rookery``
command::

    rookery enqueue <parcel-id> [--prompt <path>] [--deps <id1,id2>] [--priority <N>]
    rookery list [--status pending|claimed|done|failed|blocked|all]
    rookery status <parcel-id>
    rookery cancel <parcel-id>
    rookery requeue <parcel-id>
    rookery summary
    rookery daemon-stop
    rookery reclaim

Daemon entry point::

    rookery-daemon start                 # canonical — dedicated console_script
    rookery daemon-start           # alias, same code path
"""

from __future__ import annotations

from rookery.orchestrator.backend import (
    AuditReport,
    AuditVerdict,
    Job,
    LandEvent,
    LandOutcome,
    LandPhase,
    MergeBlockReason,
    OrchestratorBackend,
    WorkerHandle,
)
from rookery.orchestrator.config import OrchestratorConfig
from rookery.orchestrator.orchestrator import (
    MAX_AUDIT_ITER,
    JobNotFound,
    Orchestrator,
)

__all__ = [
    "MAX_AUDIT_ITER",
    "AuditReport",
    "AuditVerdict",
    "Job",
    "JobNotFound",
    "LandEvent",
    "LandOutcome",
    "LandPhase",
    "MergeBlockReason",
    "Orchestrator",
    "OrchestratorBackend",
    "OrchestratorConfig",
    "WorkerHandle",
]
