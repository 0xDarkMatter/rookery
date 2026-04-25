"""claude-fleet — persistent parcel-dispatch queue + async daemon for unattended parallel `claude -p` sessions."""

__version__ = "0.1.0"

from claude_fleet.adapters import (
    ExitCodeAdapter,
    JsonResultAdapter,
    MarkerFileAdapter,
    VerdictAdapter,
    VerdictResult,
    get_verdict_adapter,
)

__all__ = [
    "__version__",
    "ExitCodeAdapter",
    "JsonResultAdapter",
    "MarkerFileAdapter",
    "VerdictAdapter",
    "VerdictResult",
    "get_verdict_adapter",
]
