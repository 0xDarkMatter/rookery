"""Core Orchestrator: persistent queue mechanics + lease/reclaim semantics.

Transport-free. The orchestrator itself does not spawn workers; it hands
jobs to an injected :class:`OrchestratorBackend`. The daemon loop
(:mod:`rookery.orchestrator.daemon`) is the sole consumer of the claim /
heartbeat / reclaim surface.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from rookery.orchestrator.backend import (
    AuditReport,
    AuditVerdict,
    Job,
    JobStatus,
    LandEvent,
    LandOutcome,
    LandPhase,
    MergeBlockReason,
)
from rookery.orchestrator.schema import apply_migrations, open_connection

log = structlog.get_logger(__name__)

JournalEmit = Callable[[dict[str, Any]], None]

# Hard cap on audit-and-fix cycles. Mirrors scripts/auto-feedback-loop.sh::MAX_ITER.
# Change both sides together if this ever moves.
MAX_AUDIT_ITER = 3

_VALID_VERDICTS: frozenset[str] = frozenset(
    {"PASS", "PASS_WITH_WARNINGS", "BLOCK", "UNKNOWN"}
)

_VALID_MERGE_BLOCK_REASONS: frozenset[str] = frozenset(
    {"rebase-conflict", "tests-failed", "non-ff", "timeout", "other"}
)

_VALID_LAND_PHASES: frozenset[str] = frozenset(
    {"start", "rebase", "tests", "ff", "done"}
)

_VALID_LAND_OUTCOMES: frozenset[str] = frozenset(
    {"ok", "conflict", "failed", "timeout", "skipped", "non-ff"}
)

# Status transitions allowed by the state machine. The daemon enforces these
# via `transition()`. Entries omitted here must go through mark_done /
# mark_failed / requeue / begin_landing / mark_landed / mark_merge_blocked,
# which wrap the same set with side-effects.
_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    "running": frozenset({"auditing"}),
    "auditing": frozenset({"audited"}),
    "audited": frozenset({"fixing", "blocked", "done"}),
    "fixing": frozenset({"auditing"}),
}

_JOB_COLUMNS = (
    "id",
    "prompt_path",
    "deps_json",
    "status",
    "priority",
    "claimed_by",
    "claimed_at",
    "lease_expires",
    "attempts",
    "max_attempts",
    "last_error",
    "result_json",
    "enqueued_at",
    "started_at",
    "completed_at",
    "created_by",
    "notes",
    "verification_enabled",
    "audit_iter",
    "audit_verdict",
    "parent_job_id",
    "land_attempts",
    "landed_commit",
    "merge_block_reason",
    "verdict_adapter",
)


def _row_to_job(row: sqlite3.Row) -> Job:
    """Convert a ``jobs`` row to a :class:`Job`. Defensive on missing fields."""

    def _dt(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            # SQLite CURRENT_TIMESTAMP emits 'YYYY-MM-DD HH:MM:SS' (naive UTC).
            try:
                return datetime.fromisoformat(value.replace(" ", "T"))
            except ValueError:
                return None
        return None

    deps_raw = row["deps_json"] or "[]"
    try:
        deps = list(json.loads(deps_raw))
    except (TypeError, ValueError):
        deps = []

    result_raw = row["result_json"]
    result: dict[str, object] | None = None
    if result_raw:
        try:
            parsed = json.loads(result_raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            result = parsed

    verdict_raw = row["audit_verdict"]
    verdict: AuditVerdict | None = (
        verdict_raw if verdict_raw in _VALID_VERDICTS else None
    )

    reason_raw = row["merge_block_reason"]
    reason: MergeBlockReason | None = (
        reason_raw if reason_raw in _VALID_MERGE_BLOCK_REASONS else None
    )

    # verdict_adapter is NULL for jobs created before migration 0005.
    verdict_adapter_raw = row["verdict_adapter"]

    return Job(
        id=row["id"],
        prompt_path=row["prompt_path"],
        deps=deps,
        status=row["status"],
        priority=row["priority"],
        claimed_by=row["claimed_by"],
        claimed_at=_dt(row["claimed_at"]),
        lease_expires=_dt(row["lease_expires"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        last_error=row["last_error"],
        result=result,
        enqueued_at=_dt(row["enqueued_at"]),
        started_at=_dt(row["started_at"]),
        completed_at=_dt(row["completed_at"]),
        created_by=row["created_by"],
        notes=row["notes"],
        verification_enabled=bool(row["verification_enabled"]),
        audit_iter=row["audit_iter"],
        audit_verdict=verdict,
        parent_job_id=row["parent_job_id"],
        land_attempts=row["land_attempts"],
        landed_commit=row["landed_commit"],
        merge_block_reason=reason,
        verdict_adapter=verdict_adapter_raw if isinstance(verdict_adapter_raw, str) else None,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    """ISO-8601 string in UTC. SQLite compares these lexicographically."""
    return dt.astimezone(UTC).isoformat()


class JobNotFound(LookupError):  # noqa: N818 — lookup-like names conventional
    """Raised when a queue operation targets an unknown job id."""


class LandRetryError(ValueError):
    """Raised when ``retry_land`` preconditions are not met.

    Distinct from ``ValueError`` so CLI callers can produce the right exit code
    without inspecting the message text.
    """


class Orchestrator:
    """Persistent queue with atomic claim, lease reclaim, and retry escalation.

    Thread-safe within a single process via ``_conn_lock``; cross-process
    safety comes from SQLite's ``BEGIN IMMEDIATE`` plus a 10s ``busy_timeout``.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        lease_ttl_s: int = 1800,
        journal_emit: JournalEmit | None = None,
    ) -> None:
        self.db_path = db_path
        self.lease_ttl = timedelta(seconds=lease_ttl_s)
        self._journal_emit = journal_emit
        self._conn_lock = threading.RLock()
        apply_migrations(db_path)
        self._conn = open_connection(db_path)

    # --- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        with self._conn_lock:
            self._conn.close()

    # --- journal -------------------------------------------------------------

    def emit_journal(self, event: dict[str, Any]) -> None:
        """Forward *event* to the injected ``journal_emit`` sink.

        Public so the daemon can emit ``queue.tick`` summaries without
        reaching for a private attribute. A broken sink is logged and
        swallowed — queue operations must never fail because observability
        misbehaved.
        """

        event.setdefault("ts", _iso(_now()))
        if self._journal_emit is not None:
            try:
                self._journal_emit(event)
            except Exception:
                log.exception("orchestrator.journal_emit_failed", payload=event)

    # Internal alias retained for the rest of this module.
    _emit = emit_journal

    def _log_event(
        self,
        job_id: str,
        event: str,
        *,
        actor: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._conn_lock:
            self._conn.execute(
                "INSERT INTO job_events (job_id, event, actor, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    job_id,
                    event,
                    actor,
                    json.dumps(payload) if payload else None,
                ),
            )

    # --- write API -----------------------------------------------------------

    def enqueue(
        self,
        job_id: str,
        prompt_path: str,
        *,
        deps: list[str] | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        created_by: str | None = None,
        notes: str = "",
        verification_enabled: bool = True,
        parent_job_id: str | None = None,
        verdict_adapter: str | None = None,
    ) -> Job:
        """Insert a new job. Raises sqlite3.IntegrityError on duplicate id.

        ``verification_enabled=True`` (default) opts the job into the build
        → audit → fix state machine. Callers that want the legacy
        ``running → done`` path (e.g. runtime trial dispatches) should pass
        ``verification_enabled=False``.

        ``verdict_adapter`` is the per-parcel override for the verdict adapter
        (G4). When ``None``, the global config default (``marker-file``) is
        used at harvest time. Set from parcel frontmatter ``verdict_adapter:``
        key.
        """

        deps_list = list(deps or [])
        with self._conn_lock:
            self._conn.execute(
                "INSERT INTO jobs "
                "(id, prompt_path, deps_json, priority, max_attempts, "
                "created_by, notes, verification_enabled, parent_job_id, "
                "verdict_adapter) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    prompt_path,
                    json.dumps(deps_list),
                    priority,
                    max_attempts,
                    created_by,
                    notes or None,
                    1 if verification_enabled else 0,
                    parent_job_id,
                    verdict_adapter,
                ),
            )
            self._log_event(
                job_id,
                "enqueue",
                actor=created_by or "cli",
                payload={
                    "deps": deps_list,
                    "priority": priority,
                    "verification_enabled": verification_enabled,
                    "parent_job_id": parent_job_id,
                    "verdict_adapter": verdict_adapter,
                },
            )
        self._emit(
            {
                "kind": "queue.enqueue",
                "job_id": job_id,
                "deps": deps_list,
                "priority": priority,
                "verification_enabled": verification_enabled,
            }
        )
        return self.status(job_id)

    def cancel(self, job_id: str) -> Job:
        """Mark a job as failed, noting it was cancelled. Idempotent on terminal jobs."""

        with self._conn_lock:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            if row["status"] in {"done", "failed", "blocked"}:
                return self.status(job_id)
            self._conn.execute(
                "UPDATE jobs SET status='failed', last_error='cancelled', "
                "completed_at=CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )
            self._log_event(job_id, "cancel", actor="cli")
        self._emit({"kind": "queue.cancel", "job_id": job_id})
        return self.status(job_id)

    def requeue(self, job_id: str) -> Job:
        """Reset a failed/blocked job back to pending with attempts=0."""

        with self._conn_lock:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            self._conn.execute(
                "UPDATE jobs SET status='pending', claimed_by=NULL, claimed_at=NULL, "
                "lease_expires=NULL, attempts=0, last_error=NULL, "
                "started_at=NULL, completed_at=NULL, result_json=NULL "
                "WHERE id = ?",
                (job_id,),
            )
            self._log_event(job_id, "requeue", actor="cli")
        self._emit({"kind": "queue.requeue", "job_id": job_id})
        return self.status(job_id)

    # --- read API ------------------------------------------------------------

    def status(self, job_id: str) -> Job:
        with self._conn_lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return _row_to_job(row)

    def list_jobs(
        self, status: JobStatus | list[JobStatus] | None = None
    ) -> list[Job]:
        """List jobs, optionally filtered by one or more statuses.

        ``status=None`` returns everything. A single status or a list of
        statuses filters with ``status IN (...)``. Empty list returns [].
        """
        with self._conn_lock:
            if status is None:
                rows = self._conn.execute(
                    f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs "
                    "ORDER BY priority DESC, enqueued_at ASC"
                ).fetchall()
            elif isinstance(status, list):
                if not status:
                    return []
                placeholders = ",".join("?" * len(status))
                rows = self._conn.execute(
                    f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs "
                    f"WHERE status IN ({placeholders}) "
                    "ORDER BY priority DESC, enqueued_at ASC",
                    tuple(status),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE status = ? "
                    "ORDER BY priority DESC, enqueued_at ASC",
                    (status,),
                ).fetchall()
        return [_row_to_job(r) for r in rows]

    def summary(self) -> dict[str, int]:
        """Return a status-rollup dict with every status key always present."""

        out: dict[str, int] = {
            "pending": 0,
            "claimed": 0,
            "running": 0,
            "done": 0,
            "failed": 0,
            "blocked": 0,
            "auditing": 0,
            "audited": 0,
            "fixing": 0,
            "landing": 0,
            "landed": 0,
            "merge-blocked": 0,
        }
        with self._conn_lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        for row in rows:
            out[row["status"]] = row["n"]
        return out

    # --- internal (daemon-only) ---------------------------------------------

    def claim_next(self, worker_id: str) -> Job | None:
        """Atomic claim. Returns the claimed Job, or None if nothing ready.

        Uses ``BEGIN IMMEDIATE`` to acquire the SQLite RESERVED lock before
        reading, so two concurrent daemons cannot claim the same job. A failed
        claim (the targeted job was taken between SELECT and UPDATE) returns
        None — the caller should simply retry on the next tick.
        """

        now = _now()
        expires = now + self.lease_ttl

        with self._conn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                return None
            try:
                row = self._conn.execute(
                    f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs_ready "
                    "ORDER BY priority DESC, enqueued_at ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None

                cursor = self._conn.execute(
                    "UPDATE jobs "
                    "SET status='claimed', "
                    "    claimed_by=?, "
                    "    claimed_at=?, "
                    "    lease_expires=?, "
                    "    attempts=attempts+1, "
                    "    started_at=COALESCE(started_at, ?) "
                    "WHERE id=? AND status='pending'",
                    (
                        worker_id,
                        _iso(now),
                        _iso(expires),
                        _iso(now),
                        row["id"],
                    ),
                )
                if cursor.rowcount != 1:
                    # Lost the race — another daemon claimed it between SELECT
                    # and UPDATE. BEGIN IMMEDIATE should make this impossible,
                    # but treat it as soft-fail just in case.
                    self._conn.execute("COMMIT")
                    return None

                self._conn.execute(
                    "INSERT INTO job_events (job_id, event, actor, payload_json) "
                    "VALUES (?, 'claim', ?, ?)",
                    (
                        row["id"],
                        "daemon",
                        json.dumps({"worker_id": worker_id}),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

            refreshed = self._conn.execute(
                f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()

        job = _row_to_job(refreshed)
        self._emit(
            {
                "kind": "queue.claim",
                "job_id": job.id,
                "worker_id": worker_id,
                "attempt": job.attempts,
            }
        )
        return job

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        """Extend the lease for a job currently held by *worker_id*."""

        now = _now()
        expires = now + self.lease_ttl
        with self._conn_lock:
            cursor = self._conn.execute(
                "UPDATE jobs SET lease_expires=?, status=? "
                "WHERE id=? AND claimed_by=? AND status IN ('claimed','running')",
                (_iso(expires), "running", job_id, worker_id),
            )
            if cursor.rowcount:
                self._log_event(
                    job_id,
                    "heartbeat",
                    actor=worker_id,
                    payload={"lease_expires": _iso(expires)},
                )

    def mark_done(
        self, job_id: str, result: dict[str, object], *, worker_id: str | None = None
    ) -> None:
        with self._conn_lock:
            self._conn.execute(
                "UPDATE jobs SET status='done', completed_at=CURRENT_TIMESTAMP, "
                "result_json=?, lease_expires=NULL WHERE id=?",
                (json.dumps(result), job_id),
            )
            self._log_event(
                job_id,
                "complete",
                actor=worker_id or "daemon",
                payload={"result": result},
            )
        self._emit(
            {
                "kind": "queue.done",
                "job_id": job_id,
                "worker_id": worker_id,
                "result": result,
            }
        )

    def mark_failed(
        self, job_id: str, error: str, *, worker_id: str | None = None
    ) -> None:
        """Mark a job as failed. If attempts < max_attempts, flip back to pending."""

        with self._conn_lock:
            row = self._conn.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            attempts = row["attempts"]
            cap = row["max_attempts"]

            if attempts >= cap:
                self._conn.execute(
                    "UPDATE jobs SET status='blocked', last_error=?, "
                    "completed_at=CURRENT_TIMESTAMP, lease_expires=NULL, "
                    "claimed_by=NULL WHERE id=?",
                    (error, job_id),
                )
                self._log_event(
                    job_id,
                    "block",
                    actor=worker_id or "daemon",
                    payload={"error": error, "attempts": attempts},
                )
                self._emit(
                    {
                        "kind": "queue.block",
                        "job_id": job_id,
                        "reason": "max-attempts",
                        "attempts": attempts,
                    }
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET status='pending', last_error=?, "
                    "lease_expires=NULL, claimed_by=NULL, claimed_at=NULL "
                    "WHERE id=?",
                    (error, job_id),
                )
                self._log_event(
                    job_id,
                    "fail",
                    actor=worker_id or "daemon",
                    payload={"error": error, "attempt": attempts},
                )
                self._emit(
                    {
                        "kind": "queue.fail",
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "error": error,
                        "attempt": attempts,
                    }
                )

    # --- audit integration --------------------------------------------------

    def transition(
        self,
        job_id: str,
        new_status: JobStatus,
        *,
        worker_id: str | None = None,
    ) -> Job:
        """Move *job_id* to *new_status* if the transition is permitted.

        Transitions allowed:

        - ``running → auditing``    (build reached PARCEL_DONE; enter audit)
        - ``auditing → audited``    (audit produced a verdict; handoff)
        - ``audited → fixing``      (verdict required fixer)
        - ``audited → done``        (verdict PASS; we're finished)
        - ``audited → blocked``     (verdict BLOCK at MAX_AUDIT_ITER; stuck)
        - ``fixing → auditing``     (fixer finished; re-audit)

        Transitions into / out of the pre-audit lifecycle
        (``pending → claimed → running → done/failed``) are owned by the
        queue's existing methods (``claim_next``, ``mark_done``,
        ``mark_failed``) and are NOT accepted here. ``ValueError`` on any
        other pair.
        """

        with self._conn_lock:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            current: JobStatus = row["status"]

            allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
            if new_status not in allowed:
                raise ValueError(
                    f"illegal transition: {current} -> {new_status} "
                    f"(allowed from {current}: {sorted(allowed)})"
                )

            # Terminal transitions also stamp completed_at and release the
            # lease so reclaim_expired doesn't trip on them.
            if new_status in {"done", "blocked"}:
                self._conn.execute(
                    "UPDATE jobs SET status=?, completed_at=CURRENT_TIMESTAMP, "
                    "lease_expires=NULL WHERE id=?",
                    (new_status, job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET status=? WHERE id=?",
                    (new_status, job_id),
                )

            self._log_event(
                job_id,
                f"transition:{new_status}",
                actor=worker_id or "daemon",
                payload={"from": current, "to": new_status},
            )

        self._emit(
            {
                "kind": "queue.transition",
                "job_id": job_id,
                "from": current,
                "to": new_status,
            }
        )
        return self.status(job_id)

    def attach_audit_result(
        self,
        job_id: str,
        iter_: int,
        verdict: AuditVerdict,
        report_path: Path,
    ) -> Job:
        """Record an audit iteration's outcome against *job_id*.

        Inserts a row into ``audit_reports`` and updates the job's
        ``audit_iter`` / ``audit_verdict`` projection columns so callers
        don't need to join for the common "what's the latest verdict?"
        query. The ``UNIQUE (job_id, iter)`` constraint guards against
        double-ingesting the same iteration.
        """

        if verdict not in _VALID_VERDICTS:
            raise ValueError(f"unknown verdict: {verdict!r}")
        if iter_ < 1:
            raise ValueError(f"iter must be >= 1 (got {iter_})")

        report_str = str(report_path)
        with self._conn_lock:
            row = self._conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)

            self._conn.execute(
                "INSERT INTO audit_reports (job_id, iter, verdict, report_path) "
                "VALUES (?, ?, ?, ?)",
                (job_id, iter_, verdict, report_str),
            )
            self._conn.execute(
                "UPDATE jobs SET audit_iter=?, audit_verdict=? WHERE id=?",
                (iter_, verdict, job_id),
            )
            self._log_event(
                job_id,
                "audit",
                actor="daemon",
                payload={
                    "iter": iter_,
                    "verdict": verdict,
                    "report_path": report_str,
                },
            )

        self._emit(
            {
                "kind": "queue.audit",
                "job_id": job_id,
                "iter": iter_,
                "verdict": verdict,
                "report_path": report_str,
            }
        )
        return self.status(job_id)

    def audit_history(self, job_id: str) -> list[AuditReport]:
        """Return every audit iteration recorded for *job_id*, oldest first."""

        with self._conn_lock:
            exists = self._conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if exists is None:
                raise JobNotFound(job_id)
            rows = self._conn.execute(
                "SELECT job_id, iter, verdict, report_path, created_at "
                "FROM audit_reports WHERE job_id = ? ORDER BY iter ASC",
                (job_id,),
            ).fetchall()

        reports: list[AuditReport] = []
        for r in rows:
            created_raw = r["created_at"]
            created: datetime | None
            if isinstance(created_raw, datetime):
                created = created_raw
            elif isinstance(created_raw, str):
                try:
                    created = datetime.fromisoformat(created_raw.replace(" ", "T"))
                except ValueError:
                    created = None
            else:
                created = None
            reports.append(
                AuditReport(
                    job_id=r["job_id"],
                    iter=r["iter"],
                    verdict=r["verdict"],
                    report_path=Path(r["report_path"]),
                    created_at=created,
                )
            )
        return reports

    # --- auto-land integration ----------------------------------------------

    def begin_landing(self, job_id: str) -> Job:
        """Transition a PASS-verdict job from ``audited`` to ``landing``.

        Increments ``land_attempts`` so ``land_events`` rows and audit-trail
        queries can be scoped per-attempt. Raises ``ValueError`` unless the
        job is currently ``audited`` with a ``PASS`` verdict — land attempts
        without a clean audit are a bug, not a soft-fail path.
        """

        with self._conn_lock:
            row = self._conn.execute(
                "SELECT status, audit_verdict, land_attempts FROM jobs "
                "WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            if row["status"] != "audited":
                raise ValueError(
                    f"begin_landing requires status='audited', got "
                    f"{row['status']!r}"
                )
            if row["audit_verdict"] != "PASS":
                raise ValueError(
                    f"begin_landing requires verdict='PASS', got "
                    f"{row['audit_verdict']!r}"
                )

            next_attempts = row["land_attempts"] + 1
            self._conn.execute(
                "UPDATE jobs SET status='landing', land_attempts=? "
                "WHERE id=?",
                (next_attempts, job_id),
            )
            self._log_event(
                job_id,
                "land:start",
                actor="daemon",
                payload={"attempt": next_attempts},
            )

        self._emit(
            {
                "kind": "queue.land.start",
                "job_id": job_id,
                "attempt": next_attempts,
            }
        )
        return self.status(job_id)

    def mark_landed(self, job_id: str, commit: str) -> Job:
        """Move a ``landing`` job to the terminal ``landed`` state.

        *commit* is the SHA of ``main`` after the fast-forward. Stored on the
        job so the CLI / audit dashboards don't need to re-derive it from the
        land-events trail.
        """

        if not commit:
            raise ValueError("mark_landed requires a non-empty commit sha")

        with self._conn_lock:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            if row["status"] != "landing":
                raise ValueError(
                    f"mark_landed requires status='landing', got "
                    f"{row['status']!r}"
                )

            self._conn.execute(
                "UPDATE jobs SET status='landed', landed_commit=?, "
                "completed_at=CURRENT_TIMESTAMP, lease_expires=NULL "
                "WHERE id=?",
                (commit, job_id),
            )
            self._log_event(
                job_id,
                "land:done",
                actor="daemon",
                payload={"commit": commit},
            )

        self._emit(
            {
                "kind": "queue.land.done",
                "job_id": job_id,
                "commit": commit,
            }
        )
        return self.status(job_id)

    def mark_merge_blocked(
        self,
        job_id: str,
        reason: MergeBlockReason,
        detail: str | None = None,
    ) -> Job:
        """Move a ``landing`` job to the terminal ``merge-blocked`` state.

        *reason* is one of the enumerated failure modes; anything else is a
        programming error and raises ``ValueError``. *detail* is free-text
        stashed on ``last_error`` so the existing CLI surfaces render it
        without a schema change.
        """

        if reason not in _VALID_MERGE_BLOCK_REASONS:
            raise ValueError(
                f"unknown merge_block reason: {reason!r} "
                f"(valid: {sorted(_VALID_MERGE_BLOCK_REASONS)})"
            )

        with self._conn_lock:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            if row["status"] != "landing":
                raise ValueError(
                    f"mark_merge_blocked requires status='landing', got "
                    f"{row['status']!r}"
                )

            self._conn.execute(
                "UPDATE jobs SET status='merge-blocked', "
                "merge_block_reason=?, last_error=?, "
                "completed_at=CURRENT_TIMESTAMP, lease_expires=NULL "
                "WHERE id=?",
                (reason, detail, job_id),
            )
            self._log_event(
                job_id,
                "land:blocked",
                actor="daemon",
                payload={"reason": reason, "detail": detail},
            )

        self._emit(
            {
                "kind": "queue.land.blocked",
                "job_id": job_id,
                "reason": reason,
                "detail": detail,
            }
        )
        return self.status(job_id)

    def retry_land(self, job_id: str, worktree_base: Path | None = None) -> Job:
        """Retry landing for a ``merge-blocked`` job.

        Preconditions
        -------------
        1. Job must exist (else :exc:`JobNotFound`).
        2. ``job.status == "merge-blocked"`` (else :exc:`LandRetryError` — exit 1).
        3. Worktree must exist on disk at ``worktree_base / job_id`` when
           *worktree_base* is provided (else :exc:`LandRetryError` with
           "worktree missing — re-enqueue" guidance — exit 7 at CLI layer).

        On success
        ----------
        - ``merge_block_reason`` cleared to ``NULL``
        - ``land_attempts`` incremented
        - ``status`` set to ``"landing"``
        - Job event ``land:retry`` logged

        The daemon's ``_start_land_attempts`` loop will then pick up the
        now-``landing`` job on the next tick and spawn the land chain.
        """

        with self._conn_lock:
            row = self._conn.execute(
                "SELECT status, land_attempts FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)

            if row["status"] != "merge-blocked":
                raise LandRetryError(
                    f"land retry requires status='merge-blocked', got "
                    f"{row['status']!r} for job {job_id!r}"
                )

            # Worktree check (optional gate — only when caller supplies base).
            if worktree_base is not None:
                worktree = Path(worktree_base) / job_id
                if not worktree.exists():
                    raise LandRetryError(
                        f"worktree missing for job {job_id!r} at {worktree} — "
                        "re-enqueue the job to recreate the worktree"
                    )

            next_attempts = row["land_attempts"] + 1
            self._conn.execute(
                "UPDATE jobs SET status='landing', merge_block_reason=NULL, "
                "last_error=NULL, land_attempts=?, completed_at=NULL "
                "WHERE id=?",
                (next_attempts, job_id),
            )
            self._log_event(
                job_id,
                "land:retry",
                actor="cli",
                payload={"attempt": next_attempts},
            )

        self._emit(
            {
                "kind": "queue.land.retry",
                "job_id": job_id,
                "attempt": next_attempts,
            }
        )
        return self.status(job_id)

    def record_land_event(
        self,
        job_id: str,
        attempt: int,
        phase: LandPhase,
        outcome: LandOutcome,
        *,
        detail: str | None = None,
        commit_sha: str | None = None,
    ) -> LandEvent:
        """Append one row to ``land_events`` for *job_id*.

        Called by the :class:`LandBackend` subprocess chain at each phase
        boundary. Validates the (phase, outcome) enums against the CHECK
        constraints so callers get a clean Python error instead of an
        ``sqlite3.IntegrityError`` wrapped in an asyncio task.
        """

        if phase not in _VALID_LAND_PHASES:
            raise ValueError(f"unknown land phase: {phase!r}")
        if outcome not in _VALID_LAND_OUTCOMES:
            raise ValueError(f"unknown land outcome: {outcome!r}")
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1 (got {attempt})")

        with self._conn_lock:
            exists = self._conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if exists is None:
                raise JobNotFound(job_id)

            cursor = self._conn.execute(
                "INSERT INTO land_events "
                "(job_id, attempt, phase, outcome, detail, commit_sha) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, attempt, phase, outcome, detail, commit_sha),
            )
            new_id = cursor.lastrowid
            row = self._conn.execute(
                "SELECT job_id, attempt, phase, outcome, detail, commit_sha, "
                "created_at FROM land_events WHERE id = ?",
                (new_id,),
            ).fetchone()

        created_raw = row["created_at"]
        created: datetime | None
        if isinstance(created_raw, datetime):
            created = created_raw
        elif isinstance(created_raw, str):
            try:
                created = datetime.fromisoformat(created_raw.replace(" ", "T"))
            except ValueError:
                created = None
        else:
            created = None

        event = LandEvent(
            job_id=row["job_id"],
            attempt=row["attempt"],
            phase=row["phase"],
            outcome=row["outcome"],
            detail=row["detail"],
            commit_sha=row["commit_sha"],
            created_at=created,
        )
        self._emit(
            {
                "kind": "queue.land.event",
                "job_id": job_id,
                "attempt": attempt,
                "phase": phase,
                "outcome": outcome,
                "detail": detail,
                "commit_sha": commit_sha,
            }
        )
        return event

    def land_history(self, job_id: str) -> list[LandEvent]:
        """Return every land-events row for *job_id*, oldest first."""

        with self._conn_lock:
            exists = self._conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if exists is None:
                raise JobNotFound(job_id)
            rows = self._conn.execute(
                "SELECT job_id, attempt, phase, outcome, detail, commit_sha, "
                "created_at FROM land_events WHERE job_id = ? "
                "ORDER BY id ASC",
                (job_id,),
            ).fetchall()

        events: list[LandEvent] = []
        for r in rows:
            created_raw = r["created_at"]
            created: datetime | None
            if isinstance(created_raw, datetime):
                created = created_raw
            elif isinstance(created_raw, str):
                try:
                    created = datetime.fromisoformat(created_raw.replace(" ", "T"))
                except ValueError:
                    created = None
            else:
                created = None
            events.append(
                LandEvent(
                    job_id=r["job_id"],
                    attempt=r["attempt"],
                    phase=r["phase"],
                    outcome=r["outcome"],
                    detail=r["detail"],
                    commit_sha=r["commit_sha"],
                    created_at=created,
                )
            )
        return events

    def reclaim_expired(self) -> list[str]:
        """Move claimed/running jobs whose lease has expired back to pending.

        Jobs that have already hit ``max_attempts`` flip to ``blocked`` instead.
        Returns the list of affected job ids.
        """

        now_iso = _iso(_now())
        affected: list[str] = []

        with self._conn_lock:
            rows = self._conn.execute(
                "SELECT id, attempts, max_attempts FROM jobs "
                "WHERE status IN ('claimed','running') "
                "AND lease_expires IS NOT NULL AND lease_expires < ?",
                (now_iso,),
            ).fetchall()

            for row in rows:
                job_id = row["id"]
                attempts = row["attempts"]
                cap = row["max_attempts"]
                if attempts >= cap:
                    self._conn.execute(
                        "UPDATE jobs SET status='blocked', last_error='lease-expired', "
                        "completed_at=CURRENT_TIMESTAMP, lease_expires=NULL, "
                        "claimed_by=NULL WHERE id=?",
                        (job_id,),
                    )
                    self._log_event(
                        job_id,
                        "block",
                        actor="daemon",
                        payload={"reason": "lease-expired-and-exhausted"},
                    )
                    self._emit(
                        {
                            "kind": "queue.block",
                            "job_id": job_id,
                            "reason": "lease-expired",
                            "attempts": attempts,
                        }
                    )
                else:
                    self._conn.execute(
                        "UPDATE jobs SET status='pending', claimed_by=NULL, "
                        "claimed_at=NULL, lease_expires=NULL "
                        "WHERE id=?",
                        (job_id,),
                    )
                    self._log_event(
                        job_id,
                        "reclaim",
                        actor="daemon",
                        payload={"attempts": attempts},
                    )
                    self._emit(
                        {
                            "kind": "queue.reclaim",
                            "job_id": job_id,
                            "reason": "lease-expired",
                        }
                    )
                affected.append(job_id)

        return affected


__all__ = [
    "MAX_AUDIT_ITER",
    "JobNotFound",
    "JournalEmit",
    "LandRetryError",
    "Orchestrator",
]
