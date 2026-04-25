"""Orchestrator: persistent parcel-dispatch queue.

The orchestrator is the dispatcher that decides *what* parcel to launch
and *when*. Actual worker spawning lives in
:mod:`axiom.orchestrator.worker_backend`; landing (merge to main) lives
in :mod:`axiom.orchestrator.land_backend`.

Public surface (stable for downstream parcels)::

    from axiom.orchestrator import Orchestrator, OrchestratorConfig
    from axiom.orchestrator.backend import OrchestratorBackend, Job, WorkerHandle
    from axiom.orchestrator.worker_backend import WorkerBackend

CLI (Typer) wired into :mod:`axiom.cli` as ``axiom queue ...``::

    axiom queue enqueue <parcel-id> [--prompt <path>] [--deps <id1,id2>] [--priority <N>]
    axiom queue list [--status pending|claimed|done|failed|blocked|all]
    axiom queue status <parcel-id>
    axiom queue cancel <parcel-id>
    axiom queue requeue <parcel-id>
    axiom queue summary
    axiom queue daemon-stop
    axiom queue reclaim

Daemon entry point:

    axiomd start                      # canonical — dedicated console_script
    axiom queue daemon-start          # legacy alias, same code path
"""

from __future__ import annotations

from axiom.orchestrator.backend import (
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
from axiom.orchestrator.config import OrchestratorConfig
from axiom.orchestrator.orchestrator import (
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
