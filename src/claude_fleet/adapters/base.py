"""Base types for the verdict adapter system (G4).

:class:`VerdictResult` is the uniform outcome type returned by every adapter.
:class:`VerdictAdapter` is the ABC every adapter must implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from claude_fleet.orchestrator.backend import AuditVerdict


class VerdictResult(BaseModel):
    """Normalised result produced by any :class:`VerdictAdapter`.

    Attributes:
        verdict: One of PASS / PASS_WITH_WARNINGS / BLOCK / UNKNOWN.
        summary: Optional human-readable one-liner from the worker.
        detail: Adapter-specific structured extras (e.g. raw JSON payload,
            exit code, file path).  Intentionally open-ended so adapters
            can surface whatever is useful for debugging without needing a
            schema change.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: AuditVerdict
    summary: str | None = None
    detail: dict[str, object] = Field(default_factory=dict)


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
