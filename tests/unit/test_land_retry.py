"""Unit tests for G9 — ``land retry`` command.

Covers:
- retry_land on merge-blocked job → merge_block_reason cleared, land_attempts
  incremented, status back to ``landing``
- retry_land on non-merge-blocked job → raises LandRetryError
- retry_land on missing worktree → raises LandRetryError with re-enqueue msg
- CLI happy path (exit 0, message printed)
- CLI non-merge-blocked → exit 1
- CLI missing worktree → exit 7
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rookery.orchestrator.orchestrator import (
    JobNotFound,
    LandRetryError,
    Orchestrator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orch(tmp_path: Path) -> Orchestrator:
    db = tmp_path / "test_land_retry.db"
    return Orchestrator(db, lease_ttl_s=600)


def _enqueue_merge_blocked(orch: Orchestrator, job_id: str) -> None:
    """Enqueue a job and fast-path it to merge-blocked via direct SQL."""
    orch.enqueue(job_id, f"/tmp/{job_id}.md")
    with orch._conn_lock:  # noqa: SLF001
        orch._conn.execute(  # noqa: SLF001
            "UPDATE jobs SET status='audited', audit_verdict='PASS' WHERE id=?",
            (job_id,),
        )
    orch.begin_landing(job_id)
    orch.mark_merge_blocked(job_id, "rebase-conflict", detail="some conflict")


# ---------------------------------------------------------------------------
# Orchestrator unit tests
# ---------------------------------------------------------------------------


class TestRetryLandOrchestrator:
    def test_retry_merge_blocked_happy_path(self, tmp_path: Path) -> None:
        """retry_land on a merge-blocked job resets state and sets landing."""
        orch = _make_orch(tmp_path)
        try:
            _enqueue_merge_blocked(orch, "job-retry-01")
            before = orch.status("job-retry-01")
            assert before.status == "merge-blocked"
            assert before.merge_block_reason == "rebase-conflict"
            prev_attempts = before.land_attempts

            updated = orch.retry_land("job-retry-01")

            assert updated.status == "landing"
            assert updated.merge_block_reason is None
            assert updated.land_attempts == prev_attempts + 1
            # last_error should be cleared too
            assert updated.last_error is None
        finally:
            orch.close()

    def test_retry_non_merge_blocked_raises(self, tmp_path: Path) -> None:
        """retry_land on a job that is not merge-blocked raises LandRetryError."""
        orch = _make_orch(tmp_path)
        try:
            orch.enqueue("job-retry-02", "/tmp/job-retry-02.md")
            # Job is in 'pending' — not merge-blocked.
            with pytest.raises(LandRetryError, match="merge-blocked"):
                orch.retry_land("job-retry-02")
        finally:
            orch.close()

    def test_retry_done_status_raises(self, tmp_path: Path) -> None:
        """retry_land on a 'done' job also raises LandRetryError."""
        orch = _make_orch(tmp_path)
        try:
            orch.enqueue("job-retry-03", "/tmp/job-retry-03.md")
            orch.mark_done("job-retry-03", {"status": "done"})
            with pytest.raises(LandRetryError, match="merge-blocked"):
                orch.retry_land("job-retry-03")
        finally:
            orch.close()

    def test_retry_missing_worktree_raises(self, tmp_path: Path) -> None:
        """retry_land raises LandRetryError with re-enqueue hint when worktree absent."""
        orch = _make_orch(tmp_path)
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        try:
            _enqueue_merge_blocked(orch, "job-retry-04")
            # Worktree dir does NOT exist under worktree_base.
            with pytest.raises(LandRetryError, match="re-enqueue"):
                orch.retry_land("job-retry-04", worktree_base=worktree_base)
        finally:
            orch.close()

    def test_retry_existing_worktree_succeeds(self, tmp_path: Path) -> None:
        """retry_land succeeds when the worktree directory exists."""
        orch = _make_orch(tmp_path)
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        try:
            _enqueue_merge_blocked(orch, "job-retry-05")
            # Create the worktree directory so the check passes.
            (worktree_base / "job-retry-05").mkdir()

            updated = orch.retry_land("job-retry-05", worktree_base=worktree_base)
            assert updated.status == "landing"
        finally:
            orch.close()

    def test_retry_increments_land_attempts(self, tmp_path: Path) -> None:
        """Each retry increments land_attempts independently."""
        orch = _make_orch(tmp_path)
        try:
            _enqueue_merge_blocked(orch, "job-retry-06")
            after_first_block = orch.status("job-retry-06")
            assert after_first_block.land_attempts == 1

            # First retry.
            after_retry = orch.retry_land("job-retry-06")
            assert after_retry.land_attempts == 2

            # Simulate another block then retry.
            with orch._conn_lock:  # noqa: SLF001
                orch._conn.execute(  # noqa: SLF001
                    "UPDATE jobs SET status='merge-blocked', merge_block_reason='tests-failed' "
                    "WHERE id=?",
                    ("job-retry-06",),
                )
            after_second_retry = orch.retry_land("job-retry-06")
            assert after_second_retry.land_attempts == 3
        finally:
            orch.close()

    def test_retry_unknown_job_raises_job_not_found(self, tmp_path: Path) -> None:
        """retry_land on a non-existent job raises JobNotFound."""
        orch = _make_orch(tmp_path)
        try:
            with pytest.raises(JobNotFound):
                orch.retry_land("no-such-job")
        finally:
            orch.close()


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestLandRetryCLI:
    """CLI-level tests using Typer's CliRunner."""

    def _db_path(self, tmp_path: Path, name: str = "cli_retry.db") -> Path:
        return tmp_path / name

    def test_cli_happy_path_exit_0(self, tmp_path: Path) -> None:
        """Happy path: merge-blocked job → exit 0, success message printed."""
        from rookery.cli import app  # noqa: PLC0415

        db = self._db_path(tmp_path)
        orch = Orchestrator(db, lease_ttl_s=600)
        try:
            _enqueue_merge_blocked(orch, "cli-retry-01")
        finally:
            orch.close()

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["--db", str(db), "land", "retry", "cli-retry-01"],
        )
        assert result.exit_code == 0, f"stdout={result.output!r}"
        assert "land retry queued" in result.output
        assert "cli-retry-01" in result.output

        # Verify the DB state changed.
        orch2 = Orchestrator(db, lease_ttl_s=600)
        try:
            job = orch2.status("cli-retry-01")
            assert job.status == "landing"
            assert job.merge_block_reason is None
        finally:
            orch2.close()

    def test_cli_non_merge_blocked_exit_1(self, tmp_path: Path) -> None:
        """Non-merge-blocked job → exit 1."""
        from rookery.cli import app  # noqa: PLC0415

        db = self._db_path(tmp_path, "cli_retry_non_blocked.db")
        orch = Orchestrator(db, lease_ttl_s=600)
        try:
            orch.enqueue("cli-retry-02", "/tmp/cli-retry-02.md")
            # Job stays in 'pending'.
        finally:
            orch.close()

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["--db", str(db), "land", "retry", "cli-retry-02"],
        )
        assert result.exit_code == 1
        # Error message should mention the status constraint.
        combined = (result.output or "") + (result.stderr or "")
        assert "merge-blocked" in combined.lower() or "merge-blocked" in combined

    def test_cli_missing_worktree_exit_7(self, tmp_path: Path) -> None:
        """Missing worktree with --worktree-base supplied → exit 7."""
        from rookery.cli import app  # noqa: PLC0415

        db = self._db_path(tmp_path, "cli_retry_missing_wt.db")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        orch = Orchestrator(db, lease_ttl_s=600)
        try:
            _enqueue_merge_blocked(orch, "cli-retry-03")
        finally:
            orch.close()

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "--db", str(db),
                "land", "retry",
                "--worktree-base", str(worktree_base),
                "cli-retry-03",
            ],
        )
        assert result.exit_code == 7, f"expected 7, got {result.exit_code}; output={result.output!r}"
        combined = (result.output or "") + (result.stderr or "")
        assert "re-enqueue" in combined.lower() or "missing" in combined.lower()

    def test_cli_job_not_found_exit_4(self, tmp_path: Path) -> None:
        """Non-existent job → exit 4."""
        from rookery.cli import app  # noqa: PLC0415

        db = self._db_path(tmp_path, "cli_retry_notfound.db")
        # Create the DB (apply migrations) by opening an orch and closing it.
        Orchestrator(db, lease_ttl_s=600).close()

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["--db", str(db), "land", "retry", "does-not-exist"],
        )
        assert result.exit_code == 4
