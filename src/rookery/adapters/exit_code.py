"""ExitCodeAdapter — verdict from worker process exit code (G4).

Design note (v0.1 constraint)
------------------------------
The :class:`WorkerBackend` does **not** currently expose a synchronous
exit-code query.  The backend's ``harvest()`` checks file existence; the
liveness probe (``is_alive``) is async and uses ``STILL_ACTIVE`` / signal-0.
Neither path surfaces the raw OS exit code to the adapter layer.

Until the backend is extended in v0.2 to stash the exit code on
:class:`WorkerHandle` (or via a ``get_exit_code(pid)`` helper), this adapter
relies on a *caller-injected exit-code resolver*:

    ``exit_code_fn(job_id: str) -> int | None``

The callable returns:
- An integer when the worker has exited (0 = success = PASS, non-zero = BLOCK).
- ``None`` when the worker is still alive.

Tests supply a ``lambda`` or ``MagicMock``; production callers will wire in
the backend's exit-code query once v0.2 lands.

Mapping:
- exit code 0   → PASS
- exit code ≠ 0 → BLOCK
- None           → None  (worker still running)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rookery.adapters.base import VerdictAdapter, VerdictResult


class ExitCodeAdapter(VerdictAdapter):
    """Verdict from worker process exit code.

    Parameters
    ----------
    exit_code_fn:
        Callable ``(job_id: str) -> int | None``.  Return the exit code when
        the process has exited, ``None`` if it is still running.

        **v0.1 stub**: pass a lambda or mock in tests.  Production wiring is
        deferred to v0.2 once :class:`WorkerBackend` exposes exit codes.

    Example::

        exit_codes: dict[str, int] = {}

        adapter = ExitCodeAdapter(exit_code_fn=exit_codes.get)
        result = adapter.detect(worktree, job_id)
    """

    def __init__(self, exit_code_fn: Callable[[str], int | None]) -> None:
        self._exit_code_fn = exit_code_fn

    def detect(self, worktree: Path, job_id: str) -> VerdictResult | None:  # noqa: ARG002
        """Return verdict based on exit code, or ``None`` if still running.

        *worktree* is accepted for interface compatibility but not used —
        the exit-code signal is obtained via ``exit_code_fn``.
        """
        code = self._exit_code_fn(job_id)
        if code is None:
            return None  # worker still alive

        if code == 0:
            return VerdictResult(
                verdict="PASS",
                detail={"exit_code": code},
            )
        return VerdictResult(
            verdict="BLOCK",
            detail={"exit_code": code},
        )


__all__ = ["ExitCodeAdapter"]
