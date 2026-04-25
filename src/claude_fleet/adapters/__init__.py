"""Pluggable verdict adapter package (G4).

Public surface::

    from claude_fleet.adapters import (
        VerdictResult,
        VerdictAdapter,
        MarkerFileAdapter,
        ExitCodeAdapter,
        JsonResultAdapter,
        get_verdict_adapter,
        UnknownVerdictAdapter,
    )
"""

from claude_fleet.adapters.base import VerdictAdapter, VerdictResult
from claude_fleet.adapters.exit_code import ExitCodeAdapter
from claude_fleet.adapters.json_result import JsonResultAdapter
from claude_fleet.adapters.marker_file import MarkerFileAdapter
from claude_fleet.adapters.registry import (
    VERDICT_ADAPTERS,
    UnknownVerdictAdapter,
    get_verdict_adapter,
)

__all__ = [
    "VERDICT_ADAPTERS",
    "ExitCodeAdapter",
    "JsonResultAdapter",
    "MarkerFileAdapter",
    "UnknownVerdictAdapter",
    "VerdictAdapter",
    "VerdictResult",
    "get_verdict_adapter",
]
