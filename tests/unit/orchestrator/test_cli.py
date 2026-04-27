"""Typer CliRunner tests for ``rookery ...``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rookery.cli import app


@pytest.fixture()
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    """Isolate the CLI by redirecting its default db_path into tmp_path."""
    monkeypatch.chdir(tmp_path)
    return CliRunner()


def _db_args(tmp_path: Path) -> list[str]:
    """Return --db flag pointing to a temp db."""
    return ["--db", str(tmp_path / "cli.db")]


def test_help_lists_subcommands(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("enqueue", "list", "status", "cancel", "requeue", "summary"):
        assert sub in result.output


def test_enqueue_and_status(runner: CliRunner, tmp_path: Path) -> None:
    r = runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A", "--prompt", "/tmp/a.md"])
    assert r.exit_code == 0, r.output
    assert "enqueued" in r.output

    r = runner.invoke(app, [*_db_args(tmp_path), "status", "A"])
    assert r.exit_code == 0
    assert '"id":"A"' in r.output.replace(" ", "") or '"id": "A"' in r.output


def test_status_unknown_exits_nonzero(runner: CliRunner, tmp_path: Path) -> None:
    r = runner.invoke(app, [*_db_args(tmp_path), "status", "nope"])
    assert r.exit_code != 0
    combined = r.output + (r.stderr if r.stderr is not None else "")
    assert "no such job" in combined


def test_list_shows_enqueued_jobs(runner: CliRunner, tmp_path: Path) -> None:
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A", "--priority", "5"])
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "B"])
    r = runner.invoke(app, [*_db_args(tmp_path), "list"])
    assert r.exit_code == 0
    assert "A" in r.output
    assert "B" in r.output


def test_list_status_filter(runner: CliRunner, tmp_path: Path) -> None:
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A"])
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "B"])
    runner.invoke(app, [*_db_args(tmp_path), "cancel", "B"])

    r = runner.invoke(app, [*_db_args(tmp_path), "list", "--status", "pending"])
    assert "A" in r.output
    assert "B" not in r.output


def test_summary_shows_all_statuses(runner: CliRunner, tmp_path: Path) -> None:
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A"])
    r = runner.invoke(app, [*_db_args(tmp_path), "summary"])
    assert r.exit_code == 0
    for s in ("pending", "claimed", "running", "done", "failed", "blocked"):
        assert s in r.output


def test_cancel_then_requeue_reopens_job(runner: CliRunner, tmp_path: Path) -> None:
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A"])
    r = runner.invoke(app, [*_db_args(tmp_path), "cancel", "A"])
    assert r.exit_code == 0

    r = runner.invoke(app, [*_db_args(tmp_path), "status", "A"])
    assert '"status":"failed"' in r.output.replace(" ", "")

    r = runner.invoke(app, [*_db_args(tmp_path), "requeue", "A"])
    assert r.exit_code == 0
    r = runner.invoke(app, [*_db_args(tmp_path), "status", "A"])
    assert '"status":"pending"' in r.output.replace(" ", "")


def test_reclaim_is_safe_on_empty(runner: CliRunner, tmp_path: Path) -> None:
    r = runner.invoke(app, [*_db_args(tmp_path), "reclaim"])
    assert r.exit_code == 0
    assert "reclaimed 0" in r.output


def test_enqueue_deps_comma_list(runner: CliRunner, tmp_path: Path) -> None:
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A"])
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "B"])
    r = runner.invoke(app, [*_db_args(tmp_path), "enqueue", "C", "--deps", "A,B"])
    assert r.exit_code == 0

    r = runner.invoke(app, [*_db_args(tmp_path), "status", "C"])
    assert '"deps":["A","B"]' in r.output.replace(" ", "") or \
        '"deps": ["A", "B"]' in r.output


def test_daemon_stop_reads_default_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``daemon-stop`` defaults to reading ``rookery.pid``."""
    from rookery.cli import daemon as daemon_mod

    sent: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))

    monkeypatch.setattr(daemon_mod.os, "kill", _fake_kill)

    pidfile = tmp_path / "orch.pid"
    pidfile.write_text("12345", encoding="utf-8")

    runner = CliRunner()
    r = runner.invoke(
        app, ["daemon-stop", "--pidfile", str(pidfile)]
    )
    assert r.exit_code == 0, r.output
    assert sent == [(12345, __import__("signal").SIGTERM)]
    assert "sent SIGTERM to pid 12345" in r.output


def test_daemon_stop_explicit_pid_overrides_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rookery.cli import daemon as daemon_mod

    sent: list[int] = []

    def _fake_kill(pid: int, sig: int) -> None:
        sent.append(pid)

    monkeypatch.setattr(daemon_mod.os, "kill", _fake_kill)

    runner = CliRunner()
    r = runner.invoke(app, ["daemon-stop", "--pid", "777"])
    assert r.exit_code == 0, r.output
    assert sent == [777]


def test_daemon_stop_missing_pidfile_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rookery.cli import daemon as daemon_mod

    monkeypatch.setattr(
        daemon_mod.os, "kill", lambda *a, **kw: None
    )
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["daemon-stop", "--pidfile", str(tmp_path / "does-not-exist.pid")],
    )
    assert r.exit_code == 1
    assert "no pidfile" in (r.output + (r.stderr if r.stderr is not None else ""))


def test_land_history_empty_for_fresh_job(runner: CliRunner, tmp_path: Path) -> None:
    r = runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A", "--prompt", "/tmp/a.md"])
    assert r.exit_code == 0

    r = runner.invoke(app, [*_db_args(tmp_path), "land-history", "A"])
    assert r.exit_code == 0
    assert "no land attempts recorded" in r.output


def test_land_history_missing_job_exits_nonzero(
    runner: CliRunner, tmp_path: Path
) -> None:
    r = runner.invoke(app, [*_db_args(tmp_path), "land-history", "ghost"])
    assert r.exit_code != 0


def test_list_includes_land_columns(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The expanded ``list`` view must surface the land column headers even
    when no jobs are in a landing state yet."""
    r = runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A", "--prompt", "/tmp/a.md"])
    assert r.exit_code == 0
    r = runner.invoke(app, [*_db_args(tmp_path), "list"])
    assert r.exit_code == 0
    # The Rich table truncates with "…" in narrow terminals, so match on
    # stable column-header substrings rather than full words.
    assert "land" in r.output
    assert "commit" in r.output


def test_summary_includes_land_statuses(runner: CliRunner, tmp_path: Path) -> None:
    r = runner.invoke(app, [*_db_args(tmp_path), "summary"])
    assert r.exit_code == 0
    assert "landing" in r.output
    assert "landed" in r.output
    assert "merge-blocked" in r.output


def test_status_json_flag_emits_indented_payload(
    runner: CliRunner, tmp_path: Path
) -> None:
    """``status --json`` pretty-prints the job with ISO datetimes."""
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A", "--prompt", "/tmp/a.md"])
    r = runner.invoke(app, [*_db_args(tmp_path), "status", "A", "--json"])
    assert r.exit_code == 0, r.output
    # Indented → has a newline inside the payload.
    assert "\n" in r.output
    obj = json.loads(r.output)
    assert obj["id"] == "A"
    assert obj["status"] == "pending"


def test_list_json_flag_emits_jobs_array(
    runner: CliRunner, tmp_path: Path
) -> None:
    """``list --json`` emits ``{"filter": str, "jobs": [...]}``."""
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A"])
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "B", "--priority", "5"])
    r = runner.invoke(app, [*_db_args(tmp_path), "list", "--json"])
    assert r.exit_code == 0, r.output
    obj = json.loads(r.output)
    assert obj["filter"] == "active"
    ids = {j["id"] for j in obj["jobs"]}
    assert ids == {"A", "B"}


def test_list_json_honours_all_filter(runner: CliRunner, tmp_path: Path) -> None:
    runner.invoke(app, [*_db_args(tmp_path), "enqueue", "A"])
    runner.invoke(app, [*_db_args(tmp_path), "cancel", "A"])

    r = runner.invoke(app, [*_db_args(tmp_path), "list", "--all", "--json"])
    assert r.exit_code == 0, r.output
    obj = json.loads(r.output)
    assert obj["filter"] == "all"
    assert any(j["id"] == "A" and j["status"] == "failed" for j in obj["jobs"])
