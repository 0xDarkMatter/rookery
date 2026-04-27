"""rookery — persistent parcel-dispatch queue + async daemon for unattended parallel `claude -p` sessions."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("rookery")
except PackageNotFoundError:  # pragma: no cover — only when running uninstalled
    __version__ = "0.0.0+unknown"

from rookery.adapters import (
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
