"""Daemon control sub-commands for ``rookery daemon ...``.

daemon-stop    — send SIGTERM to a running daemon
daemon-status  — check liveness of a running daemon

Note: the daemon itself lives in orchestrator/__main__.py and is invoked
via the ``rookery-daemon`` console script (no subcommand — Typer
collapses the single-command app into flat options), not here.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

console = Console()

daemon_app = typer.Typer(
    name="daemon",
    help="Daemon lifecycle commands (stop, status).",
    no_args_is_help=True,
    add_completion=False,
)

_DEFAULT_PIDFILE = Path("rookery.pid")


def _resolve_pidfile(explicit: str) -> Path:
    return Path(explicit) if explicit else _DEFAULT_PIDFILE


def _read_pid_from_file(pidfile: Path) -> int | None:
    if not pidfile.exists():
        return None
    try:
        return int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Cross-platform process liveness check via psutil."""
    try:
        import psutil  # noqa: PLC0415

        return bool(psutil.pid_exists(pid))
    except ImportError:
        # Fallback: try os.kill signal 0
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _tail_lines(path: Path, *, n: int = 100) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = min(size, max(16_384, n * 256))
            fh.seek(max(0, size - chunk), 0)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


_ISO_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
)
_JOB_ID_RE = re.compile(r"job_id[\"'\s:=]+([^\"'\s,}]+)")

SPAWN_EVENT_MARKERS = (
    "orchestrator.worker_backend.spawn",
    "orchestrator.local_backend.spawn",
)

DEFAULT_DAEMON_LOG_CANDIDATES: tuple[Path, ...] = (
    Path("logs/pm2-rookery-daemon-out.log"),
    Path("logs/rookery-daemon.log"),
    Path(".data/logs/orchestrator-daemon.log"),
)


def _find_daemon_log(
    candidates: tuple[Path, ...] = DEFAULT_DAEMON_LOG_CANDIDATES,
) -> Path | None:
    existing: list[tuple[Path, float]] = []
    for p in candidates:
        try:
            stat = p.stat()
        except OSError:
            continue
        existing.append((p, stat.st_mtime))
    if not existing:
        return None
    existing.sort(key=lambda t: t[1], reverse=True)
    return existing[0][0]


def _parse_last_spawn(
    log_path: Path, *, max_lines: int = 500
) -> tuple[datetime, str] | None:
    lines = _tail_lines(log_path, n=max_lines)
    for line in reversed(lines):
        if not any(marker in line for marker in SPAWN_EVENT_MARKERS):
            continue
        ts_match = _ISO_TS_RE.search(line)
        job_match = _JOB_ID_RE.search(line)
        if not (ts_match and job_match):
            continue
        try:
            ts = datetime.fromisoformat(ts_match.group(1).replace(" ", "T"))
        except ValueError:
            continue
        return (ts, job_match.group(1))
    return None


def _pm2_jlist(pm2_name: str | None = None) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            ["pm2", "jlist"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    names = [pm2_name] if pm2_name else ["rookery-daemon", "rookery-daemon"]
    for name in names:
        for entry in data:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
    return None


_DAEMON_STATUS_KEYS: tuple[str, ...] = (
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
)


def _build_daemon_status_report(
    db_path: Path,
    *,
    pidfile: Path,
    daemon_log_override: str,
    pm2_name: str,
    stall_window_s: int,
) -> dict[str, Any]:
    pid = _read_pid_from_file(pidfile)
    alive = False
    if pid is not None:
        try:
            alive = _pid_alive(pid)
        except Exception:  # noqa: BLE001
            alive = False

    report: dict[str, Any] = {
        "status": "ALIVE" if alive else "DEAD",
        "health": "healthy" if alive else "dead",
        "pid": pid,
        "pidfile": str(pidfile),
        "process_alive": alive,
        "managed_by": "unknown",
        "pm2_name": None,
        "pm2_status": None,
        "daemon_log": None,
        "last_spawn_iso": None,
        "last_spawn_job_id": None,
        "last_spawn_age_seconds": None,
        "active_workers": 0,
        "pending_jobs": 0,
    }

    pm2_entry = _pm2_jlist(pm2_name or None)
    if pm2_entry is not None:
        report["pm2_name"] = pm2_entry.get("name")
        env = pm2_entry.get("pm2_env")
        if isinstance(env, dict):
            report["pm2_status"] = env.get("status")
        report["managed_by"] = "pm2"
    elif alive:
        report["managed_by"] = "shell"

    try:
        from rookery.orchestrator.config import OrchestratorConfig  # noqa: PLC0415
        from rookery.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415

        cfg = OrchestratorConfig(db_path=db_path)
        orch = Orchestrator(cfg.db_path, lease_ttl_s=cfg.lease_ttl_s)
        try:
            queue_summary = orch.summary()
        finally:
            orch.close()
        report["active_workers"] = sum(
            queue_summary.get(s, 0)
            for s in ("claimed", "running", "auditing", "fixing", "landing")
        )
        report["pending_jobs"] = queue_summary.get("pending", 0)
    except Exception:  # noqa: BLE001
        pass

    log_path = Path(daemon_log_override) if daemon_log_override else _find_daemon_log()
    if log_path is not None and log_path.exists():
        report["daemon_log"] = str(log_path)
        last = _parse_last_spawn(log_path)
        if last is not None:
            ts, job_id = last
            report["last_spawn_iso"] = ts.isoformat()
            report["last_spawn_job_id"] = job_id
            try:
                age = (datetime.now() - ts).total_seconds()
            except TypeError:
                age = 0.0
            report["last_spawn_age_seconds"] = max(0, int(age))

    if alive:
        age_s = report["last_spawn_age_seconds"]
        pending_count = report["pending_jobs"]
        if (
            isinstance(age_s, int)
            and isinstance(pending_count, int)
            and pending_count > 0
            and age_s > stall_window_s
        ):
            report["health"] = "stalled"

    return report


def _render_daemon_status_table(report: dict[str, Any]) -> None:
    alive = bool(report.get("process_alive"))
    colour = "green" if alive else "red"
    console.print(
        f"[{colour}]daemon: {report.get('status')}[/{colour}] "
        f"(health={report.get('health')})"
    )
    table = Table(show_header=False, box=None)
    table.add_column("key", style="dim")
    table.add_column("value")
    for key in _DAEMON_STATUS_KEYS:
        value = report.get(key)
        table.add_row(key, "-" if value is None else str(value))
    console.print(table)


def daemon_stop_cmd(
    ctx: typer.Context,
    pid: int = typer.Option(
        0,
        "--pid",
        help="Explicit pid to signal. Overrides --pidfile when non-zero.",
    ),
    pidfile: str = typer.Option(
        "",
        "--pidfile",
        help=f"Pidfile to read (default {_DEFAULT_PIDFILE}).",
    ),
) -> None:
    """Send SIGTERM to a running daemon."""
    resolved_pid = pid
    if resolved_pid == 0:
        resolved_pidfile = _resolve_pidfile(pidfile)
        try:
            contents = resolved_pidfile.read_text(encoding="utf-8").strip()
        except OSError as exc:
            typer.echo(
                f"no pidfile at {resolved_pidfile}: {exc}; pass --pid or --pidfile",
                err=True,
            )
            raise typer.Exit(code=1) from None
        try:
            resolved_pid = int(contents)
        except ValueError:
            typer.echo(
                f"pidfile {resolved_pidfile} contains non-integer: {contents!r}",
                err=True,
            )
            raise typer.Exit(code=1) from None

    try:
        os.kill(resolved_pid, signal.SIGTERM)
    except OSError as exc:
        typer.echo(f"could not signal pid {resolved_pid}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    console.print(f"sent SIGTERM to pid {resolved_pid}")


def daemon_status_cmd(
    ctx: typer.Context,
    pidfile: str = typer.Option(
        "",
        "--pidfile",
        help=f"Pidfile to read (default {_DEFAULT_PIDFILE}).",
    ),
    daemon_log: str = typer.Option(
        "",
        "--daemon-log",
        help="Daemon log to parse for last-spawn signal.",
    ),
    pm2_name: str = typer.Option(
        "",
        "--pm2-name",
        help="pm2 app name to cross-reference.",
    ),
    stall_window_s: int = typer.Option(
        900,
        "--stall-window",
        help="Seconds since last spawn beyond which we call the daemon stalled.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit structured JSON instead of a Rich report.",
    ),
) -> None:
    """Check liveness of the orchestrator daemon.

    Reads the pidfile, verifies process liveness via psutil, and reports
    the most recent spawn event from the daemon log. Exits 0 when ALIVE,
    1 when DEAD.
    """
    db_path = Path(ctx.obj["db"]) if ctx.obj and "db" in ctx.obj else Path("./rookery.db")
    report = _build_daemon_status_report(
        db_path,
        pidfile=_resolve_pidfile(pidfile),
        daemon_log_override=daemon_log,
        pm2_name=pm2_name,
        stall_window_s=stall_window_s,
    )

    if json_output:
        typer.echo(json.dumps(report, indent=2, default=str))
    else:
        _render_daemon_status_table(report)

    if not report["process_alive"]:
        raise typer.Exit(code=1)


# Also wire as direct sub-commands on the daemon_app
daemon_app.command("stop")(daemon_stop_cmd)
daemon_app.command("status")(daemon_status_cmd)


__all__ = ["daemon_app", "daemon_status_cmd", "daemon_stop_cmd"]
