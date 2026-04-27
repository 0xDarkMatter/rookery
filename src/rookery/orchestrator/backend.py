"""Pluggable backend interface for the orchestrator.

Filled in at Step 6.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal[
    "pending",
    "claimed",
    "running",
    "done",
    "failed",
    "blocked",
    "auditing",
    "audited",
    "fixing",
    "landing",
    "landed",
    "merge-blocked",
]

AuditVerdict = Literal["PASS", "PASS_WITH_WARNINGS", "BLOCK", "UNKNOWN"]

# Reasons a job can get stuck in `merge-blocked`. Mirrors the CHECK on
# jobs.merge_block_reason plus `LandResult.outcome` for non-ok paths.
MergeBlockReason = Literal[
    "rebase-conflict",
    "tests-failed",
    "non-ff",
    "timeout",
    "other",
]

# Phases of one land-attempt, in the order they execute.
LandPhase = Literal["start", "rebase", "tests", "ff", "done"]

# Outcome of one phase. `ok` means move to the next phase; anything else is
# terminal for the attempt.
LandOutcome = Literal["ok", "conflict", "failed", "timeout", "skipped", "non-ff"]


class Job(BaseModel):
    """A queued parcel execution. Mirrors a row in the ``jobs`` table."""

    model_config = ConfigDict(extra="forbid")

    id: str
    prompt_path: str
    deps: list[str] = Field(default_factory=list)
    status: JobStatus = "pending"
    priority: int = 0
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires: datetime | None = None
    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None
    result: dict[str, object] | None = None
    enqueued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_by: str | None = None
    notes: str | None = None
    verification_enabled: bool = True
    audit_iter: int = 0
    audit_verdict: AuditVerdict | None = None
    parent_job_id: str | None = None
    land_attempts: int = 0
    landed_commit: str | None = None
    merge_block_reason: MergeBlockReason | None = None
    verdict_adapter: str | None = None


class AuditReport(BaseModel):
    """One row from ``audit_reports`` — a single audit iteration's outcome."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    iter: int
    verdict: AuditVerdict
    report_path: Path
    created_at: datetime | None = None


class LandEvent(BaseModel):
    """One row from ``land_events`` — one phase of one land-attempt."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    attempt: int
    phase: LandPhase
    outcome: LandOutcome
    detail: str | None = None
    commit_sha: str | None = None
    created_at: datetime | None = None


class WorkerHandle(BaseModel):
    """Returned from spawn(); minimal info to monitor/signal the worker."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    worker_id: str
    pid: int | None = None
    worktree: Path
    log_path: Path


class OrchestratorBackend(ABC):
    """All backend implementations honour this ABC."""

    @abstractmethod
    async def spawn(self, job: Job) -> WorkerHandle:
        """Launch a worker for this job. Return handle immediately; worker runs async."""

    @abstractmethod
    async def is_alive(self, handle: WorkerHandle) -> bool:
        """Is the worker still running?"""

    @abstractmethod
    async def harvest(self, handle: WorkerHandle) -> dict[str, object] | None:
        """Return result dict if done, None if still running."""

    @abstractmethod
    async def terminate(self, handle: WorkerHandle) -> None:
        """Forcibly stop the worker."""


__all__ = [
    "AuditReport",
    "AuditVerdict",
    "Job",
    "JobStatus",
    "LandEvent",
    "LandOutcome",
    "LandPhase",
    "MergeBlockReason",
    "OrchestratorBackend",
    "WorkerHandle",
]
