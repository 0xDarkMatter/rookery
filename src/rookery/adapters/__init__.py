"""Pluggable verdict adapter package (G4 + v0.3 chain/db).

Public surface::

    from rookery.adapters import (
        VerdictResult,
        VerdictAdapter,
        MarkerFileAdapter,
        ExitCodeAdapter,
        JsonResultAdapter,
        DbResultAdapter,         # v0.3
        ChainedAdapter,          # v0.3
        get_verdict_adapter,
        UnknownVerdictAdapter,
    )
"""

from rookery.adapters.base import VerdictAdapter, VerdictResult
from rookery.adapters.chain import ChainedAdapter
from rookery.adapters.db import DbResultAdapter
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
    "ChainedAdapter",
    "DbResultAdapter",
    "ExitCodeAdapter",
    "JsonResultAdapter",
    "MarkerFileAdapter",
    "UnknownVerdictAdapter",
    "VerdictAdapter",
    "VerdictResult",
    "get_verdict_adapter",
]
