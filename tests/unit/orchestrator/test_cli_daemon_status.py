"""Tests for ``rookery daemon-status``.

Authoritative liveness check: pidfile -> psutil -> pm2 jlist (optional) ->
recent-activity window. Exit code 0 when ALIVE, 1 when DEAD.

We black-box the pure data-gathering half (:func:`_build_daemon_status_report`)
and the Typer command. psutil, pm2 subprocess, and the daemon log are all
monkeypatched so these tests don't touch the real environment.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rookery.cli import app
from rookery.cli import daemon as daemon_mod
from rookery.orchestrator.orchestrator import Orchestrator


@pytest.fixture()
def no_pm2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out ``_pm2_jlist`` so build-report tests don't shell out to pm2."""
    monkeypatch.setattr(daemon_mod, "_pm2_jlist", lambda name=None: None)


def _tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "ds.db"


def test_build_report_dead_when_no_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_pm2: None
) -> None:
    report = daemon_mod._build_daemon_status_report(
        _tmp_db(tmp_path),
        pidfile=tmp_path / "missing.pid",
        daemon_log_override="",
        pm2_name="",
        stall_window_s=900,
    )
    assert report["status"] == "DEAD"
    assert report["health"] == "dead"
    assert report["pid"] is None
    assert report["process_alive"] is False


def test_build_report_dead_when_process_not_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_pm2: None
) -> None:
    pidfile = tmp_path / "orch.pid"
    pidfile.write_text("42013", encoding="utf-8")
    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: False)

    report = daemon_mod._build_daemon_status_report(
        _tmp_db(tmp_path),
        pidfile=pidfile,
        daemon_log_override="",
        pm2_name="",
        stall_window_s=900,
    )
    assert report["status"] == "DEAD"
    assert report["pid"] == 42013
    assert report["process_alive"] is False


def test_build_report_alive_when_pid_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_pm2: None
) -> None:
    pidfile = tmp_path / "orch.pid"
    pidfile.write_text("42013", encoding="utf-8")
    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: True)

    report = daemon_mod._build_daemon_status_report(
        _tmp_db(tmp_path),
        pidfile=pidfile,
        daemon_log_override="",
        pm2_name="",
        stall_window_s=900,
    )
    assert report["status"] == "ALIVE"
    assert report["health"] == "healthy"
    assert report["pid"] == 42013
    assert report["process_alive"] is True
    # No pm2 + alive → managed_by defaults to "shell".
    assert report["managed_by"] == "shell"


def test_build_report_alive_stalled_when_last_spawn_old_and_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_pm2: None
) -> None:
    """Alive daemon + pending jobs + ancient last_spawn → health=stalled."""
    # Seed a pending job so the summary reports pending > 0.
    o = Orchestrator(_tmp_db(tmp_path))
    try:
        o.enqueue("A", prompt_path="/tmp/a.md")
    finally:
        o.close()

    pidfile = tmp_path / "orch.pid"
    pidfile.write_text("42013", encoding="utf-8")
    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: True)

    # Write a daemon log with a very old spawn line.
    log = tmp_path / "daemon.log"
    old = datetime.now() - timedelta(hours=2)
    log.write_text(
        f"{old.isoformat(timespec='seconds')} [info] "
        "orchestrator.local_backend.spawn job_id=W0-ancient worker_id=w1\n",
        encoding="utf-8",
    )

    report = daemon_mod._build_daemon_status_report(
        _tmp_db(tmp_path),
        pidfile=pidfile,
        daemon_log_override=str(log),
        pm2_name="",
        stall_window_s=900,
    )
    assert report["status"] == "ALIVE"
    assert report["health"] == "stalled"
    assert report["last_spawn_job_id"] == "W0-ancient"
    assert report["last_spawn_age_seconds"] > 900
    assert report["pending_jobs"] == 1


def test_build_report_alive_healthy_when_recent_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_pm2: None
) -> None:
    """Recent spawn event keeps the daemon 'healthy' even with pending jobs."""
    o = Orchestrator(_tmp_db(tmp_path))
    try:
        o.enqueue("A", prompt_path="/tmp/a.md")
    finally:
        o.close()

    pidfile = tmp_path / "orch.pid"
    pidfile.write_text("42013", encoding="utf-8")
    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: True)

    log = tmp_path / "daemon.log"
    recent = datetime.now() - timedelta(seconds=30)
    log.write_text(
        f"{recent.isoformat(timespec='seconds')} [info] "
        "orchestrator.local_backend.spawn job_id=W21-fresh worker_id=w1\n",
        encoding="utf-8",
    )

    report = daemon_mod._build_daemon_status_report(
        _tmp_db(tmp_path),
        pidfile=pidfile,
        daemon_log_override=str(log),
        pm2_name="",
        stall_window_s=900,
    )
    assert report["health"] == "healthy"
    assert report["last_spawn_job_id"] == "W21-fresh"


def test_parse_last_spawn_handles_json_formatted_log(tmp_path: Path) -> None:
    log = tmp_path / "json.log"
    log.write_text(
        '{"timestamp": "2026-04-24T01:39:46", '
        '"event": "orchestrator.local_backend.spawn", '
        '"job_id": "W21-auto-retire-landed-worktrees", '
        '"worker_id": "w1"}\n',
        encoding="utf-8",
    )
    result = daemon_mod._parse_last_spawn(log)
    assert result is not None
    ts, job_id = result
    assert ts == datetime(2026, 4, 24, 1, 39, 46)
    assert job_id == "W21-auto-retire-landed-worktrees"


def test_parse_last_spawn_returns_none_for_log_without_spawn(
    tmp_path: Path,
) -> None:
    log = tmp_path / "boring.log"
    log.write_text(
        "2026-04-24T01:39:46 [info] some unrelated event\n", encoding="utf-8"
    )
    assert daemon_mod._parse_last_spawn(log) is None


def test_cli_daemon_status_exit_code_0_when_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_pm2: None
) -> None:
    pidfile = tmp_path / "orch.pid"
    pidfile.write_text("42013", encoding="utf-8")

    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: True)

    runner = CliRunner()
    r = runner.invoke(
        app,
        ["--db", str(_tmp_db(tmp_path)), "daemon-status", "--pidfile", str(pidfile)],
    )
    assert r.exit_code == 0, r.output
    assert "ALIVE" in r.output


def test_cli_daemon_status_exit_code_1_when_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_pm2: None
) -> None:
    runner = CliRunner()
    r = runner.invoke(
        app,
        [
            "--db",
            str(_tmp_db(tmp_path)),
            "daemon-status",
            "--pidfile",
            str(tmp_path / "missing.pid"),
        ],
    )
    assert r.exit_code == 1
    assert "DEAD" in r.output


def test_cli_daemon_status_json_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_pm2: None
) -> None:
    pidfile = tmp_path / "orch.pid"
    pidfile.write_text("42013", encoding="utf-8")

    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: True)

    runner = CliRunner()
    r = runner.invoke(
        app,
        [
            "--db",
            str(_tmp_db(tmp_path)),
            "daemon-status",
            "--pidfile",
            str(pidfile),
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    obj = json.loads(r.output)
    # Required keys from the parcel spec.
    for key in (
        "status",
        "health",
        "pid",
        "pidfile",
        "process_alive",
        "managed_by",
        "pm2_name",
        "pm2_status",
        "daemon_log",
        "last_spawn_iso",
        "last_spawn_job_id",
        "last_spawn_age_seconds",
        "active_workers",
        "pending_jobs",
    ):
        assert key in obj, f"missing {key}"
    assert obj["status"] == "ALIVE"
    assert obj["pid"] == 42013


def test_pm2_jlist_parses_matching_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_pm2_jlist`` picks the entry whose ``name`` matches the default set."""
    payload = json.dumps(
        [
            {"name": "unrelated", "pm2_env": {"status": "online"}},
            {
                "name": "rookery-daemon",
                "pm2_env": {"status": "online", "pm_uptime": 0},
            },
        ]
    )

    class _Proc:
        returncode = 0
        stdout = payload
        stderr = ""

    monkeypatch.setattr(daemon_mod.subprocess, "run", lambda *a, **kw: _Proc())

    entry = daemon_mod._pm2_jlist()
    assert entry is not None
    assert entry["name"] == "rookery-daemon"


def test_pm2_jlist_returns_none_when_pm2_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*a: object, **kw: object) -> None:
        raise FileNotFoundError("pm2 not installed")

    monkeypatch.setattr(daemon_mod.subprocess, "run", _raise)
    assert daemon_mod._pm2_jlist() is None
