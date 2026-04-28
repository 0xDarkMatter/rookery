"""Chained verdict adapter — first non-None wins.

The default ``verdict_adapter: chain`` config in v0.3+ uses this to
prefer :class:`DbResultAdapter` (workers that invoked
``rookery parcel done``) and fall back to :class:`MarkerFileAdapter`
(legacy parcels that wrote ``PARCEL_DONE-<id>.md``).

Order matters — adapters are tried in list order.  Wrap any number of
sub-adapters; the first one that returns a non-None :class:`VerdictResult`
short-circuits the chain.
"""

from __future__ import annotations

from pathlib import Path

from rookery.adapters.base import VerdictAdapter, VerdictResult


class ChainedAdapter(VerdictAdapter):
    """Compose multiple adapters; return the first non-None result.

    All sub-adapters must already be instantiated — the chain just calls
    their ``detect()`` methods in order.  Empty chains return ``None``
    (worker still running by definition).
    """

    def __init__(self, adapters: list[VerdictAdapter]) -> None:
        self.adapters = list(adapters)

    def detect(self, worktree: Path, job_id: str) -> VerdictResult | None:
        for adapter in self.adapters:
            result = adapter.detect(worktree, job_id)
            if result is not None:
                return result
        return None


__all__ = ["ChainedAdapter"]
