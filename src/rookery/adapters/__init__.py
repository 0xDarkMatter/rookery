"""Pluggable verdict adapter package (G4).

Public surface::

    from rookery.adapters import (
        VerdictResult,
        VerdictAdapter,
        MarkerFileAdapter,
        ExitCodeAdapter,
        JsonResultAdapter,
        get_verdict_adapter,
        UnknownVerdictAdapter,
    )
"""

from rookery.adapters.base import VerdictAdapter, VerdictResult
from rookery.adapters.exit_code import ExitCodeAdapter
from rookery.adapters.json_result import JsonResultAdapter
from rookery.adapters.marker_file import MarkerFileAdapter
from rookery.adapters.registry import (
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
