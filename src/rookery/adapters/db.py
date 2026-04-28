"""Direct-DB verdict adapter (v0.3).

The default protocol after v0.3.0: workers invoke ``rookery parcel done``
which INSERTs a row into ``parcel_results``. This adapter reads that row
back at harvest time, returning a typed :class:`VerdictResult` with all
the structured metadata fields populated.

Workers that don't use the helper fall through to the next adapter in the
chain (typically :class:`MarkerFileAdapter`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from rookery.adapters.base import VerdictAdapter, VerdictResult
from rookery.orchestrator.backend import AuditVerdict


class DbResultAdapter(VerdictAdapter):
    """Detect completion by querying the ``parcel_results`` table.

    Reads the **latest attempt** for *job_id* — workers may retry within
    a single daemon-claimed attempt, in which case the most recent INSERT
    OR REPLACE wins.

    Returns ``None`` when no row exists (worker still running, or never
    invoked the helper).  Always reports ``reported_via='cli'`` in the
    detail dict so consumers can tell which adapter saw the verdict.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def detect(self, worktree: Path, job_id: str) -> VerdictResult | None:
        """Return the latest verdict row for *job_id*, or None if absent.

        ``worktree`` is unused by this adapter — the DB row is the source
        of truth.  We accept it to honour the :class:`VerdictAdapter` ABC.
        """
        # Open a fresh read-only connection per call.  The harvest tick is
        # 5s by default so this is not a hot path; opening on demand keeps
        # the adapter stateless and thread-safe.
        try:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        except sqlite3.OperationalError:
            # DB doesn't exist yet (e.g. test fixtures starting before init).
            return None

        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT verdict, summary, detail_md,
                       tokens_in, tokens_out, duration_s,
                       tests_passed, tests_failed, files_changed,
                       reported_via
                  FROM parcel_results
                 WHERE job_id = ?
                 ORDER BY attempt DESC
                 LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None

        return VerdictResult(
            verdict=cast(AuditVerdict, row["verdict"]),
            summary=row["summary"],
            detail_md=row["detail_md"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            duration_s=row["duration_s"],
            tests_passed=row["tests_passed"],
            tests_failed=row["tests_failed"],
            files_changed=row["files_changed"],
            detail={"reported_via": row["reported_via"]},
        )


__all__ = ["DbResultAdapter"]
