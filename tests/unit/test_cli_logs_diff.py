"""Tests for ``rookery logs`` and ``rookery diff`` CLI commands.

The two helpers resolve a job's on-disk artifacts (worktree + log file)
from the queue's config + the job_id and surface them with sensible
defaults.  Both validate the job exists in the queue DB before doing
filesystem work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rookery.cli import app
from rookery.orchestrator.orchestrator import Orchestrator


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_init_repo(path: Path) -> None:
    """Create a tiny git repo at *path* with a single commit on main."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )


def _setup_queue_with_job(tmp_path: Path) -> tuple[Path, Path]:
    """Create a rookery DB + worktrees layout with one enqueued job.

    Returns (db_path, worktrees_root).
    """
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    (worktrees_root / "logs").mkdir()

    db_path = tmp_path / "rookery.db"
    orch = Orchestrator(db_path, lease_ttl_s=60)
    orch.enqueue("test-parcel", "parcels/test.md")
    orch.close()

    return db_path, worktrees_root


# ---------------------------------------------------------------------------
# rookery logs
# ---------------------------------------------------------------------------


class TestRookeryLogs:
    def test_no_log_yet_friendly_message(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        db_path, _ = _setup_queue_with_job(tmp_path)
        # No log file written yet
        result = runner.invoke(
            app, ["--db", str(db_path), "logs", "test-parcel"]
        )
        assert result.exit_code == 0
        assert "no log yet" in result.output

    def test_unknown_job_clear_error(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path, _ = _setup_queue_with_job(tmp_path)
        result = runner.invoke(
            app, ["--db", str(db_path), "logs", "no-such-parcel"]
        )
        assert result.exit_code == 1
        assert "no such job" in result.output

    def test_reads_last_lines(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path, worktrees_root = _setup_queue_with_job(tmp_path)
        log_path = worktrees_root / "logs" / "test-parcel.log"
        log_path.write_text(
            "\n".join(f"line {i}" for i in range(1, 21)) + "\n", encoding="utf-8"
        )
        # Point worktrees_root resolution at our scratch dir
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rookery.yaml").write_text(
            f"db_path: {db_path}\nworktrees_root: {worktrees_root}\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            ["--db", str(db_path), "--config", str(tmp_path / "rookery.yaml"),
             "logs", "test-parcel", "--lines", "5"],
        )
        assert result.exit_code == 0, result.output
        # Last 5 lines should appear; earlier lines should not
        assert "line 20" in result.output
        assert "line 16" in result.output
        assert "line 5" not in result.output  # too far back

    def test_events_interleaved(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path, worktrees_root = _setup_queue_with_job(tmp_path)
        log_path = worktrees_root / "logs" / "test-parcel.log"
        log_path.write_text("worker line 1\n", encoding="utf-8")

        # Add a progress event
        orch = Orchestrator(db_path, lease_ttl_s=60)
        orch.append_parcel_event(
            "test-parcel", attempt=1, event_type="progress",
            label="Implemented OAuth",
        )
        orch.close()

        monkeypatch.chdir(tmp_path)
        (tmp_path / "rookery.yaml").write_text(
            f"db_path: {db_path}\nworktrees_root: {worktrees_root}\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            ["--db", str(db_path), "--config", str(tmp_path / "rookery.yaml"),
             "logs", "test-parcel", "--events"],
        )
        assert result.exit_code == 0, result.output
        assert "Implemented OAuth" in result.output
        assert "worker line 1" in result.output


# ---------------------------------------------------------------------------
# rookery diff
# ---------------------------------------------------------------------------


class TestRookeryDiff:
    def test_unknown_job_clear_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        db_path, _ = _setup_queue_with_job(tmp_path)
        result = runner.invoke(
            app, ["--db", str(db_path), "diff", "no-such-parcel"]
        )
        assert result.exit_code == 1
        assert "no such job" in result.output

    def test_no_worktree_friendly_message(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path, worktrees_root = _setup_queue_with_job(tmp_path)
        # Job exists in DB but no worktree on disk
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rookery.yaml").write_text(
            f"db_path: {db_path}\nworktrees_root: {worktrees_root}\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            ["--db", str(db_path), "--config", str(tmp_path / "rookery.yaml"),
             "diff", "test-parcel"],
        )
        assert result.exit_code == 0
        assert "no worktree" in result.output

    def test_diff_with_real_worktree(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: create a real git repo in worktrees/<id>, make a
        change on a non-main branch, ensure diff shows it."""
        db_path, worktrees_root = _setup_queue_with_job(tmp_path)
        wt = worktrees_root / "test-parcel"
        _make_init_repo(wt)
        # Create a feature branch with one extra commit
        subprocess.run(
            ["git", "-C", str(wt), "checkout", "-b", "feature/test-parcel"],
            check=True, capture_output=True,
        )
        (wt / "new_file.py").write_text("# parcel output\n")
        subprocess.run(
            ["git", "-C", str(wt), "add", "-A"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add new file"],
            check=True, capture_output=True,
        )

        monkeypatch.chdir(tmp_path)
        (tmp_path / "rookery.yaml").write_text(
            f"db_path: {db_path}\nworktrees_root: {worktrees_root}\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            ["--db", str(db_path), "--config", str(tmp_path / "rookery.yaml"),
             "diff", "test-parcel", "--name-only"],
        )
        assert result.exit_code == 0, result.output
        assert "new_file.py" in result.output

    def test_diff_stat_mode(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path, worktrees_root = _setup_queue_with_job(tmp_path)
        wt = worktrees_root / "test-parcel"
        _make_init_repo(wt)
        subprocess.run(
            ["git", "-C", str(wt), "checkout", "-b", "feature/x"],
            check=True, capture_output=True,
        )
        (wt / "f.py").write_text("# x\n")
        subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "x"], check=True, capture_output=True
        )

        monkeypatch.chdir(tmp_path)
        (tmp_path / "rookery.yaml").write_text(
            f"db_path: {db_path}\nworktrees_root: {worktrees_root}\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            ["--db", str(db_path), "--config", str(tmp_path / "rookery.yaml"),
             "diff", "test-parcel", "--stat"],
        )
        assert result.exit_code == 0
        # --stat output mentions the file count
        assert "1 file changed" in result.output or "1 files changed" in result.output


class TestHelpRendering:
    def test_logs_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["logs", "--help"])
        assert result.exit_code == 0
        assert "Tail" in result.output

    def test_diff_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["diff", "--help"])
        assert result.exit_code == 0
        assert "diff" in result.output.lower()
