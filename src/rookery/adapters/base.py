"""Base types for the verdict adapter system (G4).

:class:`VerdictResult` is the uniform outcome type returned by every adapter.
:class:`VerdictAdapter` is the ABC every adapter must implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from rookery.orchestrator.backend import AuditVerdict


class VerdictResult(BaseModel):
    """Normalised result produced by any :class:`VerdictAdapter`.

    Attributes:
        verdict: One of PASS / PASS_WITH_WARNINGS / BLOCK / UNKNOWN.
        summary: Optional human-readable one-liner from the worker.
        detail: Adapter-specific structured extras (e.g. raw JSON payload,
            exit code, file path).  Intentionally open-ended so adapters
            can surface whatever is useful for debugging without needing a
            schema change.
        detail_md: Optional free-text markdown body (long-form summary).
        tokens_in / tokens_out: Worker-reported Anthropic token counts.
        duration_s: Wall-clock duration of the parcel run, in seconds.
        tests_passed / tests_failed: Test counts the worker chose to report.
        files_changed: Number of files the worker modified in the worktree.

    All numeric metadata fields are optional — workers that don't surface
    them simply leave them ``None``.  v0.2 marker-file workers populate
    only the legacy fields (verdict / summary / detail); v0.3 ``rookery
    parcel done`` workers can populate any subset of the structured set.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: AuditVerdict
    summary: str | None = None
    detail: dict[str, object] = Field(default_factory=dict)

    # v0.3 structured metadata. All optional; default ``None`` matches the
    # legacy MarkerFileAdapter output shape so existing tests stay green.
    detail_md: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_s: float | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None
    files_changed: int | None = None


class VerdictAdapter(ABC):
    """ABC for pluggable verdict detection strategies.

    All implementations **must** be idempotent — the daemon calls
    :meth:`detect` repeatedly on every harvest tick until the worker
    completes, so no side-effects should accumulate across calls.
    """

    @abstractmethod
    def detect(self, worktree: Path, job_id: str) -> VerdictResult | None:
        """Return the verdict if available; ``None`` if the worker is still running.

        Args:
            worktree: Absolute path to the job's git worktree root.
            job_id: The canonical job identifier (used for filename lookups).

        Returns:
            A :class:`VerdictResult` when the worker has signalled completion,
            or ``None`` when it hasn't finished yet.
        """


__all__ = ["VerdictAdapter", "VerdictResult"]
