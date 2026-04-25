"""Orchestrator: persistent parcel-dispatch queue.

The orchestrator is the dispatcher that decides *what* parcel to launch
and *when*. Actual worker spawning lives in
:mod:`claude_fleet.orchestrator.worker_backend`; landing (merge to main)
lives in :mod:`claude_fleet.orchestrator.land_backend`.

Public surface (stable for downstream consumers)::

    from claude_fleet.orchestrator import Orchestrator, OrchestratorConfig
    from claude_fleet.orchestrator.backend import OrchestratorBackend, Job, WorkerHandle
    from claude_fleet.orchestrator.worker_backend import WorkerBackend

CLI (Typer) — see :mod:`claude_fleet.cli` for the top-level ``claude-fleet``
command::

    claude-fleet enqueue <parcel-id> [--prompt <path>] [--deps <id1,id2>] [--priority <N>]
    claude-fleet list [--status pending|claimed|done|failed|blocked|all]
    claude-fleet status <parcel-id>
    claude-fleet cancel <parcel-id>
    claude-fleet requeue <parcel-id>
    claude-fleet summary
    claude-fleet daemon-stop
    claude-fleet reclaim

Daemon entry point::

    claude-fleetd start                 # canonical — dedicated console_script
    claude-fleet daemon-start           # alias, same code path
"""

from __future__ import annotations

from claude_fleet.orchestrator.backend import (
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
from claude_fleet.orchestrator.config import OrchestratorConfig
from claude_fleet.orchestrator.orchestrator import (
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
