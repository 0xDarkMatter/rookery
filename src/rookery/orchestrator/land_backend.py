"""Land a parcel branch onto ``main`` via rebase / test / fast-forward.

:class:`LandBackend` is the auto-land counterpart of
:class:`rookery.orchestrator.worker_backend.WorkerBackend`: it executes a
fixed, bounded git sequence as a subprocess chain rather than spawning a
new ``claude -p`` session. The daemon hands off to it when a job reaches
terminal ``audited`` with verdict ``PASS`` and ``auto_land`` is enabled.

**Safety rails (non-negotiable, mirrored on every exception path):**

- ``git push --force`` is never invoked. The backend advances only the
  *local* ``main`` ref; pushing stays the operator's call.
- ``git worktree remove`` / ``git worktree prune`` / ``git branch -D`` are
  never invoked. The parcel's worktree survives every terminal outcome.
- ``git rebase`` runs with ``--quiet`` and is always followed by
  ``git rebase --abort`` when the chain aborts or fails.
- The fast-forward step is strictly ``git merge --ff-only``. Anything that
  would require a real merge commit returns ``outcome='non-ff'``; the
  backend never falls back to ``--no-ff``.
- The rebase base is ``origin/main`` as of a fresh ``git fetch`` at the
  start of each attempt, not local ``main`` (which may lag).
- A single per-attempt timeout (``timeout_s``) covers the entire chain;
  on hit, the active subprocess is SIGTERMed and ``git rebase --abort``
  runs during cleanup.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

import structlog

from rookery.orchestrator.backend import Job
from rookery.orchestrator.land_events import EventSink, LandHandle, LandResult

log = structlog.get_logger(__name__)

IS_WINDOWS = sys.platform == "win32"

# Bucket each subprocess call by a coarse step tag so exception handlers and
# cleanup helpers can decide what to undo.
_TERM_GRACE_S = 5


class LandBackend:
    """Execute rebase / test / fast-forward for one parcel branch.

    Parameters
    ----------
    repo_root:
        Main-repo root — the directory that owns ``main``. ``git fetch``
        and the final ``git merge --ff-only`` run here via ``git -C``.
    worktrees_root:
        Directory that holds sibling parcel worktrees. Defaults to the
        sibling of ``repo_root`` used by :class:`WorkerBackend`
        (``<repo_root>/../rookery-worktrees``). The parcel branch is assumed
        to be checked out at ``<worktrees_root>/<job.id>``, the canonical
        layout the worker-spawn pipeline creates.
    test_cmd:
        Shell-parseable command to run on the rebased branch. Defaults to
        ``uv run pytest tests/``. Tests that don't have ``uv`` on path can
        override with, say, ``"true"``.
    timeout_s:
        Per-attempt hard cap, counted from the start of the chain. Covers
        fetch + rebase + tests + FF combined. Default 30 min matches
        :class:`OrchestratorConfig.auto_land_timeout_s`.
    main_branch:
        Local main branch name. ``"main"`` in this repo; other conventions
        may differ.
    remote:
        Remote whose ``<main_branch>`` is the rebase target. ``"origin"``
        by default.
    event_sink:
        Optional callback invoked at each phase boundary with (job_id,
        attempt, phase, outcome, detail, commit_sha). Production wiring
        forwards this to :meth:`Orchestrator.record_land_event`. Tests
        pass a list-appending lambda.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        worktrees_root: Path | None = None,
        test_cmd: str = "uv run pytest tests/",
        timeout_s: int = 1800,
        main_branch: str = "main",
        remote: str = "origin",
        event_sink: EventSink | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.worktrees_root = Path(
            worktrees_root or self.repo_root.parent / "rookery-worktrees"
        )
        self.test_cmd = test_cmd
        self.timeout_s = timeout_s
        self.main_branch = main_branch
        self.remote = remote
        self._event_sink = event_sink

    # -- public surface ----------------------------------------------------

    async def spawn_land(self, job: Job) -> LandHandle:
        """Begin a land attempt. Non-blocking — returns a handle.

        The caller (daemon) must have already called
        :meth:`Orchestrator.begin_landing`, which stamps ``status='landing'``
        and bumps ``land_attempts``. The attempt number read here is the
        post-increment value.
        """

        if job.land_attempts < 1:
            raise ValueError(
                f"spawn_land requires land_attempts>=1 on {job.id!r}; call "
                "Orchestrator.begin_landing() first"
            )

        task = asyncio.create_task(
            self._run_attempt(job.id, job.land_attempts),
            name=f"land-{job.id}-{job.land_attempts}",
        )
        return LandHandle(
            job_id=job.id, attempt=job.land_attempts, task=task
        )

    async def harvest(self, handle: LandHandle) -> LandResult | None:
        """Return the terminal :class:`LandResult` or ``None`` while running.

        An unexpected exception in the attempt chain is caught here (not in
        :meth:`_run_attempt`) and surfaced as an ``outcome='timeout'``
        result with a ``"chain-error: ..."`` detail. ``timeout`` is used as
        the catch-all so the daemon has a single non-``ok`` branch to act
        on; the specific reason is readable in ``detail``.
        """

        if not handle.task.done():
            return None
        try:
            return handle.task.result()
        except asyncio.CancelledError:
            return LandResult(
                outcome="timeout",
                attempt=handle.attempt,
                detail="cancelled",
            )
        except Exception as exc:  # pragma: no cover — defensive catch
            log.exception(
                "orchestrator.land_backend.chain_error",
                job_id=handle.job_id,
                attempt=handle.attempt,
            )
            return LandResult(
                outcome="timeout",
                attempt=handle.attempt,
                detail=f"chain-error: {exc}",
            )

    async def terminate(self, handle: LandHandle) -> None:
        """Cancel the in-flight attempt and run cleanup.

        Cleanup runs ``git rebase --abort`` in the parcel worktree; if no
        rebase is in progress the command no-ops safely. The daemon invokes
        this during shutdown or when a hard timeout fires.
        """

        if not handle.task.done():
            handle.task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(handle.task, timeout=_TERM_GRACE_S)
        # Always attempt the worktree cleanup even if the task already
        # finished — a crashed chain may have left a rebase half-applied.
        await self._abort_rebase_if_any(handle.job_id)

    # -- core chain --------------------------------------------------------

    async def _run_attempt(self, job_id: str, attempt: int) -> LandResult:
        """Drive the fetch → rebase → tests → FF chain under ``timeout_s``.

        Returns a :class:`LandResult` for every expected terminal state.
        Unexpected exceptions propagate to :meth:`harvest` which classifies
        them as ``timeout``.
        """

        worktree = self._worktree(job_id)
        branch = self._branch(job_id)

        self._emit(job_id, attempt, "start", "ok")

        try:
            return await asyncio.wait_for(
                self._chain(job_id, attempt, worktree, branch),
                timeout=self.timeout_s,
            )
        except TimeoutError:
            # asyncio.wait_for raises TimeoutError (Python 3.11+). Leave the
            # repo in a known state: abort any in-flight rebase.
            await self._abort_rebase_if_any(job_id)
            self._emit(job_id, attempt, "done", "timeout", detail="timeout")
            return LandResult(
                outcome="timeout",
                attempt=attempt,
                detail=f"exceeded {self.timeout_s}s",
            )

    async def _chain(
        self,
        job_id: str,
        attempt: int,
        worktree: Path,
        branch: str,
    ) -> LandResult:
        # Step 1: fetch origin/main so the rebase base is fresh.
        fetch = await self._run_git(
            self.repo_root, ["fetch", self.remote, self.main_branch]
        )
        if fetch.returncode != 0:
            self._emit(
                job_id, attempt, "rebase", "failed",
                detail=f"fetch failed: {fetch.stderr.strip()[:500]}",
            )
            return LandResult(
                outcome="timeout",
                attempt=attempt,
                detail=f"fetch failed: {fetch.stderr.strip()[:200]}",
            )

        # Step 2: rebase parcel/<id> onto origin/main, inside the worktree.
        rebase_target = f"{self.remote}/{self.main_branch}"
        rebase = await self._run_git(
            worktree, ["rebase", "--quiet", rebase_target]
        )
        if rebase.returncode != 0:
            await self._run_git(worktree, ["rebase", "--abort"])
            conflict_detail = _short_conflict_summary(rebase.stderr, rebase.stdout)
            self._emit(
                job_id, attempt, "rebase", "conflict",
                detail=conflict_detail,
            )
            self._emit(job_id, attempt, "done", "conflict", detail=conflict_detail)
            return LandResult(
                outcome="conflict",
                attempt=attempt,
                detail=conflict_detail,
            )
        self._emit(job_id, attempt, "rebase", "ok")

        # Step 3: tests on the rebased branch.
        test_argv = shlex.split(self.test_cmd, posix=not IS_WINDOWS)
        tests = await _run_subprocess(test_argv, cwd=worktree)
        if tests.returncode != 0:
            # Per spec: leave the branch rebased so a human can inspect.
            fail_detail = _short_test_summary(tests.stderr, tests.stdout)
            self._emit(
                job_id, attempt, "tests", "failed", detail=fail_detail
            )
            self._emit(job_id, attempt, "done", "failed", detail=fail_detail)
            return LandResult(
                outcome="failed",
                attempt=attempt,
                detail=fail_detail,
            )
        self._emit(job_id, attempt, "tests", "ok")

        # Step 4: checkout main, fast-forward to the rebased branch.
        co = await self._run_git(self.repo_root, ["checkout", self.main_branch])
        if co.returncode != 0:
            detail = f"checkout {self.main_branch} failed: {co.stderr.strip()[:200]}"
            self._emit(job_id, attempt, "ff", "failed", detail=detail)
            self._emit(job_id, attempt, "done", "failed", detail=detail)
            return LandResult(
                outcome="failed",
                attempt=attempt,
                detail=detail,
            )

        ff = await self._run_git(
            self.repo_root, ["merge", "--ff-only", branch]
        )
        if ff.returncode != 0:
            head = await self._run_git(self.repo_root, ["rev-parse", "HEAD"])
            head_sha = head.stdout.strip() if head.returncode == 0 else ""
            detail = (
                f"non-ff: {self.main_branch} at {head_sha[:10] or '?'}"
            )
            self._emit(job_id, attempt, "ff", "non-ff", detail=detail)
            self._emit(job_id, attempt, "done", "non-ff", detail=detail)
            return LandResult(
                outcome="non-ff",
                attempt=attempt,
                detail=detail,
            )

        # Step 5: capture the new tip sha.
        head = await self._run_git(self.repo_root, ["rev-parse", "HEAD"])
        sha = head.stdout.strip()
        self._emit(job_id, attempt, "ff", "ok", commit_sha=sha)
        self._emit(job_id, attempt, "done", "ok", commit_sha=sha)
        return LandResult(
            outcome="ok",
            attempt=attempt,
            detail=None,
            commit=sha,
        )

    # -- helpers -----------------------------------------------------------

    def _worktree(self, job_id: str) -> Path:
        return self.worktrees_root / job_id

    @staticmethod
    def _branch(job_id: str) -> str:
        return f"parcel/{job_id}"

    async def _run_git(
        self, cwd: Path, args: Sequence[str]
    ) -> _ProcResult:
        return await _run_subprocess(["git", "-C", str(cwd), *args], cwd=cwd)

    async def _abort_rebase_if_any(self, job_id: str) -> None:
        """Idempotent rebase cleanup.

        ``git rebase --abort`` returns non-zero when there's no rebase in
        progress — we swallow that; the point is to reach a clean state,
        not to surface the inner error.
        """

        worktree = self._worktree(job_id)
        if not worktree.exists():
            return
        with suppress(Exception):
            await self._run_git(worktree, ["rebase", "--abort"])

    def _emit(
        self,
        job_id: str,
        attempt: int,
        phase: str,
        outcome: str,
        *,
        detail: str | None = None,
        commit_sha: str | None = None,
    ) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(
                job_id,
                attempt,
                phase,  # type: ignore[arg-type]
                outcome,  # type: ignore[arg-type]
                detail=detail,
                commit_sha=commit_sha,
            )
        except Exception:
            log.exception(
                "orchestrator.land_backend.sink_failed",
                job_id=job_id,
                phase=phase,
                outcome=outcome,
            )


# ---------------------------------------------------------------------------
# subprocess plumbing
# ---------------------------------------------------------------------------


class _ProcResult:
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


async def _run_subprocess(argv: Sequence[str], *, cwd: Path) -> _ProcResult:
    """Async subprocess runner that returns a simple result struct.

    Uses :func:`asyncio.create_subprocess_exec` so the caller can wrap the
    whole chain in :func:`asyncio.wait_for`; on timeout the caller cancels
    the containing task and the subprocess is torn down via the task's
    cleanup. We read stdout/stderr to completion so the subprocess never
    blocks on a full pipe buffer.
    """

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await proc.communicate()
    except asyncio.CancelledError:
        # Deliberate teardown on CancelledError so the subprocess doesn't
        # linger when the enclosing task is cancelled (e.g. on timeout).
        with suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_S)
        except TimeoutError:
            with suppress(ProcessLookupError):
                proc.kill()
        raise
    return _ProcResult(
        returncode=proc.returncode or 0,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


def _short_conflict_summary(stderr: str, stdout: str) -> str:
    """Distil a conflict snippet short enough for the land_events.detail text."""
    blob = f"{stderr}\n{stdout}".strip()
    if not blob:
        return "rebase conflict"
    conflicts = [
        line.strip()
        for line in blob.splitlines()
        if "conflict" in line.lower() or line.startswith("CONFLICT")
    ]
    if conflicts:
        return " | ".join(conflicts[:3])[:500]
    return blob.splitlines()[0][:500]


def _short_test_summary(stderr: str, stdout: str) -> str:
    """Distil the first failing test from the test-runner output."""
    blob = f"{stderr}\n{stdout}".strip()
    if not blob:
        return "tests failed"
    # pytest-ish: lines like "FAILED tests/.../test_x.py::test_y"
    for line in blob.splitlines():
        if line.startswith("FAILED") or " FAILED " in line:
            return line.strip()[:500]
    # Fall back to the last non-empty line (pytest's summary sits at the end).
    tail = [line for line in blob.splitlines() if line.strip()]
    return (tail[-1] if tail else "tests failed")[:500]


__all__ = ["LandBackend"]
