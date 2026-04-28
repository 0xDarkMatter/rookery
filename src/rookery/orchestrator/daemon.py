"""Async daemon loop for the orchestrator.

Single coroutine that ticks every ``tick_interval_s``. Per tick it:

1. Reclaims expired leases (returns dead claims to ``pending``, escalates
   to ``blocked`` past ``max_attempts``).
2. Harvests every spawned worker — completion, failure, or heartbeat.
3. Fills empty slots up to ``max_concurrent`` by claiming the next ready
   job and asking the backend to spawn it.
4. Periodically emits a ``queue.tick`` summary on the journal.

Shutdown semantics: ``stop_event.set()`` triggers a graceful drain. All
spawned workers are SIGTERMed and their jobs flipped back to ``pending``
so the next daemon start picks them up — they were interrupted, not
broken.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import structlog

from rookery.adapters.base import VerdictAdapter, VerdictResult
from rookery.adapters.registry import get_verdict_adapter
from rookery.orchestrator.backend import (
    MergeBlockReason,
    OrchestratorBackend,
    WorkerHandle,
)
from rookery.orchestrator.land_backend import LandBackend
from rookery.orchestrator.land_events import LandHandle
from rookery.orchestrator.notifications import Notifier, NullNotifier
from rookery.orchestrator.orchestrator import Orchestrator
from rookery.orchestrator.retire import can_auto_retire
from rookery.orchestrator.retire import retire as retire_worktree
from rookery.worktree import WorktreeLifecycle

log = structlog.get_logger(__name__)

# How many ticks between summary emissions when nothing has changed. Keeps
# the journal from drowning in queue.tick chatter.
_SUMMARY_QUIET_TICKS = 12


def _safe_log_exception(event: str, /, **kwargs: object) -> None:
    """Wrapper around ``log.exception`` that survives encoding failures.

    Background: structlog's stderr writer calls ``print()`` on the active
    stream. On Windows the default stream uses cp1252, so a traceback
    containing non-ASCII bytes (smart quotes, box-drawing, emoji from a
    failing subprocess) raises ``UnicodeEncodeError`` *inside the logging
    call*. That exception then unwinds past the daemon's ``except Exception:
    log.exception(...)`` outer handler — silently killing the daemon.

    This helper swallows logging failures and falls back to a sanitised
    stderr write so an unrenderable error message can never take the
    daemon down. Caller still owns the actual error-handling decision
    (mark_failed, requeue, etc.).
    """
    try:
        log.exception(event, **kwargs)
    except Exception as logging_exc:
        try:
            payload = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
            msg = f"{event}: {payload} (logging_failed={logging_exc!r})\n"
            sys.stderr.buffer.write(msg.encode("utf-8", errors="replace"))
            sys.stderr.buffer.flush()
        except Exception:
            # If even the fallback fails, drop the message rather than die.
            pass


class Daemon:
    """Lifetime-bound state for one ``run_daemon`` invocation.

    Kept as a class (not module globals) so two daemons in the same process
    — handy for tests — don't trample each other's handle dicts.
    """

    def __init__(
        self,
        orch: Orchestrator,
        backend: OrchestratorBackend,
        *,
        tick_interval_s: float = 5.0,
        max_concurrent: int = 8,
        worker_id_factory: Callable[[], str] | None = None,
        auto_land: bool = False,
        land_backend: LandBackend | None = None,
        notifier: Notifier | None = None,
        auto_retire: bool = False,
        retire_worktrees_root: Path | None = None,
        retire_repo_root: Path | None = None,
        retire_project_root: Path | None = None,
        retire_idle_minutes: int = 60,
        retire_batch_size: int = 1,
        retire_main_branch: str = "main",
        default_verdict_adapter: str = "marker-file",
        # G2: WorktreeLifecycle-based immediate retire on landed transition.
        # Distinct from the W21 sweep (retire_worktrees_root path).
        lifecycle: WorktreeLifecycle | None = None,
        worktree_base: Path | None = None,
        retire_only_after_landed: bool = True,
        auto_commit_on_pass: bool = True,
    ) -> None:
        self.orch = orch
        self.backend = backend
        self.tick_interval_s = tick_interval_s
        self.max_concurrent = max_concurrent
        self._worker_id_factory = worker_id_factory
        self._handles: dict[str, WorkerHandle] = {}  # job_id → handle
        self._land_handles: dict[str, LandHandle] = {}  # job_id → land handle
        self._tick_count = 0
        self._last_summary: dict[str, int] | None = None
        # auto_land defaults to OFF. Flipping ON requires a LandBackend be
        # injected; without one, the flag degrades to no-op (same as OFF).
        # This preserves the W10 ``audited → done`` legacy path bit-for-bit.
        self.auto_land = auto_land and land_backend is not None
        self.land_backend = land_backend
        self.notifier: Notifier = notifier or NullNotifier()

        # auto_retire (W21) uses the same opt-in posture as auto_land. The
        # flag degrades to OFF if the three path anchors aren't provided —
        # without them, the 5-gate check has nothing to evaluate against.
        self.auto_retire = (
            auto_retire
            and retire_worktrees_root is not None
            and retire_repo_root is not None
            and retire_project_root is not None
        )
        self._retire_worktrees_root = retire_worktrees_root
        self._retire_repo_root = retire_repo_root
        self._retire_project_root = retire_project_root
        self._retire_idle_seconds = retire_idle_minutes * 60
        self._retire_batch_size = max(1, retire_batch_size)
        self._retire_main_branch = retire_main_branch
        # G4: default verdict adapter name used when a job has no per-parcel
        # override.  Resolved to an adapter instance via
        # _get_verdict_adapter_for_job() at harvest time.
        self._default_verdict_adapter = default_verdict_adapter

        # G2: immediate lifecycle retire on the ``landed`` transition.
        # Fires inside _harvest_land_handles() after mark_landed(), before the
        # notifier. ``lifecycle`` is the WorktreeLifecycle that owns the
        # worktrees; ``worktree_base`` is the root directory of per-job
        # worktree subdirs (used to resolve the worktree path from job.id).
        # ``retire_only_after_landed`` defaults True for safety — setting
        # False would allow retiring on other terminal states, which is
        # intentionally kept disabled.
        self._lifecycle = lifecycle
        self._worktree_base = worktree_base
        self._retire_only_after_landed = retire_only_after_landed
        # G2 auto-retire via lifecycle fires when BOTH auto_retire is True AND
        # a lifecycle is supplied with a worktree_base.
        self._lifecycle_auto_retire = (
            auto_retire
            and lifecycle is not None
            and worktree_base is not None
        )
        # Auto-commit on PASS: when enabled, the daemon stages and commits any
        # unstaged changes in the parcel worktree after a PASS / PASS_WITH_WARNINGS
        # verdict is harvested.  Opt-out via OrchestratorConfig.auto_commit_on_pass.
        self._auto_commit_on_pass = auto_commit_on_pass

    # --- main loop -----------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Tick until ``stop_event`` fires, then drain in-flight workers."""

        log.info(
            "orchestrator.daemon.start",
            tick_interval_s=self.tick_interval_s,
            max_concurrent=self.max_concurrent,
        )
        try:
            while not stop_event.is_set():
                try:
                    await self._tick()
                except Exception:
                    # Never let one bad tick kill the daemon.
                    _safe_log_exception("orchestrator.daemon.tick_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.tick_interval_s
                    )
        finally:
            await self._drain()
            log.info("orchestrator.daemon.stopped")

    # --- per-tick work -------------------------------------------------------

    async def _tick(self) -> None:
        self._tick_count += 1

        reclaimed = self.orch.reclaim_expired()
        if reclaimed:
            log.info("orchestrator.daemon.reclaimed", job_ids=reclaimed)

        await self._harvest_running()
        if self.auto_land:
            await self._start_land_attempts()
            await self._harvest_land_handles()
        if self.auto_retire:
            self._harvest_retire()
        await self._fill_slots()
        self._maybe_emit_summary()

    # --- auto-commit on PASS -------------------------------------------------

    @staticmethod
    def _subject_from_verdict_result(
        result: VerdictResult,
        job_id: str,  # noqa: ARG004 — kept for parity / future log context
    ) -> str:
        """Derive a short commit subject from the typed verdict result.

        v0.3 path: prefer ``result.summary`` (clean one-liner the worker
        provided directly).  When summary is missing/empty, look in
        ``result.detail_md`` for a ``## Summary`` block (legacy markdown
        bodies workers wrote via ``--detail-file`` or marker files).  Falls
        back to a generic message when both are absent.

        Output is truncated to 72 chars so it fits a conventional commit
        subject without wrapping.
        """
        if result.summary and result.summary.strip():
            return result.summary.strip()[:72]
        body = result.detail_md or ""
        if body:
            _summary_re = re.compile(
                r"^##\s+summary\s*$(.+?)^(?:#|\Z)",
                re.IGNORECASE | re.MULTILINE | re.DOTALL,
            )
            m = _summary_re.search(body)
            if m:
                for line in m.group(1).splitlines():
                    stripped = line.strip().lstrip("-* \t")
                    if stripped:
                        return stripped[:72]
        return "autonomous build by rookery"

    async def _maybe_auto_commit(
        self,
        job_id: str,
        worktree: Path,
        result: VerdictResult,
    ) -> None:
        """Stage and commit any unstaged work in *worktree* after a PASS verdict.

        Steps:
        1. Check verdict directly on the typed result; return early if not
           PASS / PASS_WITH_WARNINGS.
        2. ``git add -A`` — stage everything not gitignored.
        3. ``git diff --cached --quiet`` — if nothing staged, worker committed
           themselves; skip to avoid a duplicate empty commit.
        4. Commit with a subject derived from ``result.summary``.

        On any git failure the error is logged to ``job.last_error`` and
        surfaced in the daemon log, but the verdict transition is NOT blocked.
        """
        if not self._auto_commit_on_pass:
            return

        if result.verdict not in ("PASS", "PASS_WITH_WARNINGS"):
            return

        subject = self._subject_from_verdict_result(result, job_id)
        commit_msg = f"feat({job_id}): {subject}"

        try:
            await asyncio.to_thread(
                _run_git_auto_commit, worktree, commit_msg, job_id
            )
        except _AutoCommitError as exc:
            # Record on job but do NOT re-raise — verdict transition must proceed.
            log.warning(
                "orchestrator.daemon.auto_commit_failed",
                job_id=job_id,
                worktree=str(worktree),
                err=str(exc),
            )
            try:
                with self.orch._conn_lock:  # noqa: SLF001
                    self.orch._conn.execute(  # noqa: SLF001
                        "UPDATE jobs SET last_error=? WHERE id=?",
                        (f"auto-commit failed: {exc}", job_id),
                    )
            except Exception:
                log.exception(
                    "orchestrator.daemon.auto_commit_error_record_failed",
                    job_id=job_id,
                )
        except Exception:
            _safe_log_exception(
                "orchestrator.daemon.auto_commit_unexpected_error", job_id=job_id
            )

    def _get_verdict_adapter_for_job(self, job_id: str) -> VerdictAdapter:
        """Return the effective :class:`VerdictAdapter` for *job_id* (G4).

        Preference order (per-parcel override):
        1. ``jobs.verdict_adapter`` column (set from parcel frontmatter).
        2. ``self._default_verdict_adapter`` (from OrchestratorConfig).

        v0.3: passes ``db_path=self.orch.db_path`` so the ``chain`` and
        ``db`` adapters can find the queue DB.  Older adapters
        (``marker-file``, ``json-result``) ignore the kwarg.

        Raises:
            UnknownVerdictAdapter: if the resolved name is not registered.
        """
        adapter_name = self._default_verdict_adapter
        try:
            job = self.orch.status(job_id)
            if job.verdict_adapter:
                adapter_name = job.verdict_adapter
        except Exception:  # noqa: BLE001
            pass
        return get_verdict_adapter(adapter_name, db_path=self.orch.db_path)

    async def _harvest_running(self) -> None:
        """Per spec §5: liveness first, then harvest, then heartbeat.

        - **alive + no result**: heartbeat (extend lease).
        - **alive + result**: mark done/failed; release the slot.
        - **dead + result**: harvest still wins (the worker may have written
          ``PARCEL_DONE-<job_id>.md`` then exited cleanly).
        - **dead + no result**: ``mark_failed`` with the canonical exit
          message; the orchestrator decides between pending-retry and
          terminal-block based on attempts vs max_attempts.

        Completion signal is determined by the job's verdict adapter (G4).
        The default adapter reads ``PARCEL_DONE-<job_id>.md`` at the
        worktree root. Legacy ``PARCEL_DONE.md`` is also accepted for
        backward compatibility. See ``worker_backend.harvest``.
        """

        for job_id in list(self._handles):
            handle = self._handles[job_id]

            try:
                alive = await self.backend.is_alive(handle)
            except Exception:
                _safe_log_exception(
                    "orchestrator.daemon.is_alive_error", job_id=job_id
                )
                continue

            # v0.3: resolve the per-job adapter (honours frontmatter
            # verdict_adapter override) and pass it into the backend's
            # harvest() so the backend just runs the adapter and enriches
            # the result with diagnostics (e.g. stdout tail).
            try:
                adapter = self._get_verdict_adapter_for_job(job_id)
            except Exception as exc:
                _safe_log_exception(
                    "orchestrator.daemon.adapter_resolve_error",
                    job_id=job_id,
                    err=str(exc),
                )
                continue

            try:
                result = await self.backend.harvest(handle, adapter)
            except Exception as exc:
                _safe_log_exception(
                    "orchestrator.daemon.harvest_error",
                    job_id=job_id,
                    err=str(exc),
                )
                continue

            if result is not None:
                # The worker finished signalling — transition to done regardless
                # of verdict.  BLOCK / UNKNOWN propagate downstream to the
                # audit/landing pipeline (where they may yet end up as
                # ``blocked`` or ``failed``); the queue state machine only
                # cares that the worker reported in.
                #
                # Auto-commit-on-PASS fires only for PASS / PASS_WITH_WARNINGS
                # so failed-verdict worktrees aren't muddled with synthetic
                # commits.
                if result.verdict in ("PASS", "PASS_WITH_WARNINGS"):
                    await self._maybe_auto_commit(job_id, handle.worktree, result)
                # mark_done expects a JSON-serialisable dict; pydantic gives
                # us one with model_dump(mode='json').
                self.orch.mark_done(
                    job_id,
                    result.model_dump(mode="json"),
                    worker_id=handle.worker_id,
                )
                self._handles.pop(job_id, None)
                continue

            if not alive:
                self.orch.mark_failed(
                    job_id,
                    f"worker exited without reporting a verdict ({job_id})",
                    worker_id=handle.worker_id,
                )
                self._handles.pop(job_id, None)
            else:
                self.orch.heartbeat(job_id, handle.worker_id)

    # --- auto-land branches --------------------------------------------------

    async def _start_land_attempts(self) -> None:
        """Transition ``audited``-PASS jobs into ``landing`` + spawn.

        Also re-spawns orphaned ``landing`` jobs — jobs that are already in
        ``landing`` status but have no active :class:`LandHandle`.  This
        covers two cases:

        1. Normal path: ``audited``-PASS jobs transition via
           :meth:`Orchestrator.begin_landing`, then get spawned.
        2. Retry path (G9): ``merge-blocked`` jobs reset to ``landing`` by
           :meth:`Orchestrator.retry_land`. The next daemon tick detects them
           here and re-spawns the land chain without needing a separate code
           path in the daemon.

        Idempotent per tick — a job already owning a :class:`LandHandle` is
        always skipped regardless of how it entered ``landing``.
        """

        if self.land_backend is None:
            return

        # Path 1: audited+PASS → begin_landing → spawn.
        for job in self.orch.list_jobs(status="audited"):
            if job.audit_verdict != "PASS":
                continue
            if job.id in self._land_handles:
                continue  # already in flight
            try:
                self.orch.begin_landing(job.id)
            except Exception:
                log.exception(
                    "orchestrator.daemon.begin_landing_failed",
                    job_id=job.id,
                )
                continue
            # Refresh the job so land_attempts is current before spawn_land
            # reads it.
            refreshed = self.orch.status(job.id)
            try:
                handle = await self.land_backend.spawn_land(refreshed)
            except Exception as exc:
                log.exception(
                    "orchestrator.daemon.spawn_land_failed",
                    job_id=job.id,
                )
                self.orch.mark_merge_blocked(
                    job.id, "other", detail=f"spawn_land failed: {exc}"
                )
                self.notifier.merge_blocked(
                    job.id, "other", detail=f"spawn_land failed: {exc}"
                )
                continue
            self._land_handles[job.id] = handle

        # Path 2: orphaned landing jobs (e.g. after retry_land or daemon
        # restart) — already in ``landing`` but no handle in this daemon
        # instance.  Spawn directly without calling begin_landing() again
        # (land_attempts was already incremented by retry_land).
        for job in self.orch.list_jobs(status="landing"):
            if job.id in self._land_handles:
                continue  # already in flight
            try:
                handle = await self.land_backend.spawn_land(job)
            except Exception as exc:
                log.exception(
                    "orchestrator.daemon.spawn_land_orphan_failed",
                    job_id=job.id,
                )
                self.orch.mark_merge_blocked(
                    job.id, "other", detail=f"spawn_land failed: {exc}"
                )
                self.notifier.merge_blocked(
                    job.id, "other", detail=f"spawn_land failed: {exc}"
                )
                continue
            self._land_handles[job.id] = handle

    async def _harvest_land_handles(self) -> None:
        """Poll in-flight :class:`LandHandle`s and finalize on completion."""

        if self.land_backend is None:
            return
        for job_id in list(self._land_handles):
            handle = self._land_handles[job_id]
            try:
                result = await self.land_backend.harvest(handle)
            except Exception:
                log.exception(
                    "orchestrator.daemon.land_harvest_error",
                    job_id=job_id,
                )
                continue
            if result is None:
                continue

            if result.outcome == "ok":
                assert result.commit is not None
                self.orch.mark_landed(job_id, result.commit)
                # G2: immediate lifecycle retire on landed transition.
                # Only when auto_retire=True, a lifecycle is wired, AND
                # retire_only_after_landed=True (the safety default).
                # The retire runs AFTER mark_landed so the DB is consistent.
                # Failures are logged as warnings — they must not crash the
                # daemon or prevent the notifier from firing.
                if self._lifecycle_auto_retire and self._retire_only_after_landed:
                    await self._try_lifecycle_retire(job_id)
            else:
                reason = _outcome_to_reason(result.outcome)
                self.orch.mark_merge_blocked(
                    job_id, reason, detail=result.detail
                )
                self.notifier.merge_blocked(
                    job_id, reason, detail=result.detail
                )
            self._land_handles.pop(job_id, None)

    async def _try_lifecycle_retire(self, job_id: str) -> None:
        """Attempt G2 lifecycle retire for a freshly-landed job.

        Failures are caught and logged as warnings. The daemon must not
        crash and the notifier must always fire even if retire fails.
        """
        if self._lifecycle is None or self._worktree_base is None:
            return
        worktree = self._worktree_base / job_id
        try:
            job = self.orch.status(job_id)
        except Exception as exc:
            log.warning(
                "orchestrator.daemon.lifecycle_retire_status_failed",
                job_id=job_id,
                err=str(exc),
            )
            return
        try:
            await self._lifecycle.retire(job, worktree)
            log.info(
                "orchestrator.daemon.lifecycle_retired",
                job_id=job_id,
                worktree=str(worktree),
            )
        except Exception as exc:
            log.warning(
                "orchestrator.daemon.lifecycle_retire_failed",
                job_id=job_id,
                worktree=str(worktree),
                err=str(exc),
            )

    # --- auto-retire branch --------------------------------------------------

    def _harvest_retire(self) -> None:
        """Retire up to ``retire_batch_size`` landed-and-idle worktrees.

        Runs only when ``auto_retire`` is enabled. Each retirement runs
        the full 6-gate check (see :mod:`rookery.orchestrator.retire`).
        Gate failures are logged to the journal as
        ``worktree.retire_blocked`` with the specific reason; a
        successful retirement emits ``worktree.retired``.

        Synchronous because the work is fast (stat + two git
        subprocesses) and does not share state with the async claim
        loop. Running it inline keeps journal ordering deterministic.
        """

        if (
            self._retire_worktrees_root is None
            or self._retire_repo_root is None
            or self._retire_project_root is None
        ):
            return

        retired = 0
        for job in self.orch.list_jobs(status="landed"):
            if retired >= self._retire_batch_size:
                break
            try:
                check = can_auto_retire(
                    job,
                    worktrees_root=self._retire_worktrees_root,
                    repo_root=self._retire_repo_root,
                    idle_seconds=self._retire_idle_seconds,
                    main_branch=self._retire_main_branch,
                )
            except Exception:
                _safe_log_exception(
                    "orchestrator.daemon.retire_check_failed",
                    job_id=job.id,
                )
                continue
            if not check.ok:
                self.orch.emit_journal(
                    {
                        "kind": "worktree.retire_blocked",
                        "job_id": job.id,
                        "reason": check.reason,
                    }
                )
                continue
            try:
                result = retire_worktree(
                    job,
                    worktrees_root=self._retire_worktrees_root,
                    repo_root=self._retire_repo_root,
                    project_root=self._retire_project_root,
                    idle_seconds=self._retire_idle_seconds,
                    main_branch=self._retire_main_branch,
                )
            except Exception as exc:
                _safe_log_exception(
                    "orchestrator.daemon.retire_failed",
                    job_id=job.id,
                    err=str(exc),
                )
                self.orch.emit_journal(
                    {
                        "kind": "worktree.retire_blocked",
                        "job_id": job.id,
                        "reason": "retire_raised",
                        "detail": str(exc),
                    }
                )
                continue
            self.orch.emit_journal(
                {
                    "kind": "worktree.retired",
                    "job_id": result.job_id,
                    "path": str(result.worktree),
                    "parcel_done_archive": (
                        str(result.parcel_done_copied)
                        if result.parcel_done_copied is not None
                        else None
                    ),
                }
            )
            log.info(
                "orchestrator.daemon.retired",
                job_id=result.job_id,
                path=str(result.worktree),
            )
            retired += 1

    async def _fill_slots(self) -> None:
        slots_free = self.max_concurrent - len(self._handles)
        while slots_free > 0:
            worker_id = self._mint_worker_id()
            job = self.orch.claim_next(worker_id)
            if job is None:
                return
            try:
                handle = await self.backend.spawn(job)
            except Exception as exc:
                # Spawn failures count against the job's attempts because
                # claim_next already incremented them.
                _safe_log_exception(
                    "orchestrator.daemon.spawn_failed",
                    job_id=job.id,
                    worker_id=worker_id,
                    err=str(exc),
                )
                self.orch.mark_failed(job.id, f"spawn failed: {exc}", worker_id=worker_id)
                slots_free -= 1
                continue

            # Backend may mint its own worker_id (e.g. WorkerBackend uses a
            # local-<uuid> namespace). Stick with the orchestrator's view of
            # ``worker_id`` for journal/heartbeat correlation; the handle
            # carries the backend's id for logging.
            self._handles[job.id] = handle
            slots_free -= 1

    def _mint_worker_id(self) -> str:
        if self._worker_id_factory is not None:
            return self._worker_id_factory()
        import uuid  # noqa: PLC0415
        return f"daemon-{uuid.uuid4().hex[:8]}"

    def _maybe_emit_summary(self) -> None:
        summary = self.orch.summary()
        changed = summary != self._last_summary
        if changed or self._tick_count % _SUMMARY_QUIET_TICKS == 0:
            self.orch.emit_journal({"kind": "queue.tick", "summary": summary})
            self._last_summary = summary

    # --- shutdown ------------------------------------------------------------

    async def _drain(self) -> None:
        """Terminate every spawned worker and flip its job back to pending."""

        for job_id, handle in list(self._handles.items()):
            try:
                await self.backend.terminate(handle)
            except Exception:
                _safe_log_exception(
                    "orchestrator.daemon.terminate_error", job_id=job_id
                )
            # Restore the job to pending so the next daemon start re-runs it.
            try:
                self.orch.requeue(job_id)
            except Exception:
                _safe_log_exception(
                    "orchestrator.daemon.requeue_on_drain_failed", job_id=job_id
                )
        self._handles.clear()

        # Land handles: cancel any in-flight attempts. We do NOT roll the
        # job back to ``audited`` — the interrupted attempt already bumped
        # ``land_attempts`` and recorded partial events. The next daemon
        # start will pick the job back up (still in ``landing``) and the
        # reclaim logic / operator can re-audit if needed. Safer than
        # auto-rolling a half-applied land sequence.
        for job_id, land_handle in list(self._land_handles.items()):
            if self.land_backend is None:
                continue
            try:
                await self.land_backend.terminate(land_handle)
            except Exception:
                log.exception(
                    "orchestrator.daemon.land_terminate_error", job_id=job_id
                )
        self._land_handles.clear()


class _AutoCommitError(RuntimeError):
    """Raised by :func:`_run_git_auto_commit` when a git step fails."""


def _run_git_auto_commit(worktree: Path, commit_msg: str, job_id: str) -> None:
    """Stage all changes and commit them in *worktree* (sync, runs in a thread).

    Steps:
    1. ``git -C <worktree> add -A`` — stage everything not gitignored.
    2. ``git -C <worktree> diff --cached --quiet`` — if exit 0, nothing to
       commit (worker self-committed); return without creating an empty commit.
    3. ``git -C <worktree> commit -m <msg>`` — record the parcel work.

    Raises:
        _AutoCommitError: if ``git add`` or ``git commit`` exits non-zero.
    """
    wt = str(worktree)

    # Step 1: stage everything.
    add_result = subprocess.run(
        ["git", "-C", wt, "add", "-A"],
        capture_output=True,
        text=True,
        check=False,
    )
    if add_result.returncode != 0:
        raise _AutoCommitError(
            f"git add -A failed (exit {add_result.returncode}): "
            f"{add_result.stderr.strip()}"
        )

    # Step 2: check for staged changes.
    diff_result = subprocess.run(
        ["git", "-C", wt, "diff", "--cached", "--quiet"],
        capture_output=True,
        check=False,
    )
    if diff_result.returncode == 0:
        # Nothing staged — worker committed themselves already; nothing to do.
        log.debug(
            "orchestrator.daemon.auto_commit_skip_clean",
            job_id=job_id,
            worktree=wt,
        )
        return

    # Step 3: commit.
    commit_result = subprocess.run(
        ["git", "-C", wt, "commit", "-m", commit_msg],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_result.returncode != 0:
        raise _AutoCommitError(
            f"git commit failed (exit {commit_result.returncode}): "
            f"{commit_result.stderr.strip()}"
        )
    log.info(
        "orchestrator.daemon.auto_committed",
        job_id=job_id,
        worktree=wt,
        subject=commit_msg,
    )


def _outcome_to_reason(outcome: str) -> MergeBlockReason:
    """Map :class:`LandResult`.outcome to a ``merge_block_reason`` enum value.

    ``ok`` should never reach here (daemon gates on it before calling). Any
    unrecognised value flattens to ``other`` so a schema-level CHECK miss
    doesn't escape :class:`Orchestrator.mark_merge_blocked`.
    """

    mapping: dict[str, MergeBlockReason] = {
        "conflict": "rebase-conflict",
        "failed": "tests-failed",
        "non-ff": "non-ff",
        "timeout": "timeout",
    }
    return mapping.get(outcome, "other")


async def run_daemon(
    orch: Orchestrator,
    backend: OrchestratorBackend,
    stop_event: asyncio.Event,
    *,
    tick_interval_s: float = 5.0,
    max_concurrent: int = 8,
    worker_id_factory: Callable[[], str] | None = None,
    auto_land: bool = False,
    land_backend: LandBackend | None = None,
    notifier: Notifier | None = None,
    auto_retire: bool = False,
    retire_worktrees_root: Path | None = None,
    retire_repo_root: Path | None = None,
    retire_project_root: Path | None = None,
    retire_idle_minutes: int = 60,
    retire_batch_size: int = 1,
    retire_main_branch: str = "main",
    default_verdict_adapter: str = "marker-file",
    lifecycle: WorktreeLifecycle | None = None,
    worktree_base: Path | None = None,
    retire_only_after_landed: bool = True,
    auto_commit_on_pass: bool = True,
) -> None:
    """Top-level entrypoint. See :class:`Daemon` for behaviour."""

    daemon = Daemon(
        orch,
        backend,
        tick_interval_s=tick_interval_s,
        max_concurrent=max_concurrent,
        worker_id_factory=worker_id_factory,
        auto_land=auto_land,
        land_backend=land_backend,
        notifier=notifier,
        auto_retire=auto_retire,
        retire_worktrees_root=retire_worktrees_root,
        retire_repo_root=retire_repo_root,
        retire_project_root=retire_project_root,
        retire_idle_minutes=retire_idle_minutes,
        retire_batch_size=retire_batch_size,
        retire_main_branch=retire_main_branch,
        default_verdict_adapter=default_verdict_adapter,
        lifecycle=lifecycle,
        worktree_base=worktree_base,
        retire_only_after_landed=retire_only_after_landed,
        auto_commit_on_pass=auto_commit_on_pass,
    )
    await daemon.run(stop_event)


__all__ = ["Daemon", "run_daemon"]
