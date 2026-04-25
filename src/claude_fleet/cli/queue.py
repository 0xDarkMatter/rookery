"""Queue management commands for ``claude-fleet``.

Covers: enqueue, list, status, requeue, cancel, summary, reclaim.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from claude_fleet.orchestrator.backend import Job, JobStatus
from claude_fleet.orchestrator.orchestrator import JobNotFound, Orchestrator

console = Console()

_ACTIVE_STATUSES: list[JobStatus] = [
    "pending",
    "claimed",
    "running",
    "blocked",
    "auditing",
    "audited",
    "fixing",
    "landing",
]
_TERMINAL_STATUSES: list[JobStatus] = ["done", "failed", "landed", "merge-blocked"]

_DEFAULT_DB = Path("./claude-fleet.db")


def _open_orch(db_path: Path | None = None) -> Orchestrator:
    """Open an Orchestrator using the given db path (or the default)."""
    path = db_path or _DEFAULT_DB
    from claude_fleet.orchestrator.config import OrchestratorConfig  # noqa: PLC0415

    cfg = OrchestratorConfig(db_path=path)
    return Orchestrator(cfg.db_path, lease_ttl_s=cfg.lease_ttl_s)


def _db_from_ctx(ctx: typer.Context) -> Path:
    """Extract the --db path from the root callback context, if available."""
    if ctx.obj and "db" in ctx.obj:
        return Path(ctx.obj["db"])
    return _DEFAULT_DB


def _parse_deps(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _job_to_jsonable(job: Job) -> dict[str, Any]:
    """Pydantic Job to JSON-safe dict."""
    data: dict[str, Any] = json.loads(job.model_dump_json())
    return data


def _jobs_to_jsonable(jobs: list[Job]) -> list[dict[str, Any]]:
    return [_job_to_jsonable(j) for j in jobs]


def _render_land_status(status: str, merge_block_reason: str | None) -> str:
    if status == "landing":
        return "landing"
    if status == "landed":
        return "landed"
    if status == "merge-blocked":
        return f"blocked:{merge_block_reason or '?'}"
    return "-"


def enqueue_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Parcel id, e.g. my-task"),
    prompt: str = typer.Option(
        "",
        "--prompt",
        help="Path to parcel prompt file. Defaults to parcels/<id>.md.",
    ),
    deps: str = typer.Option(
        "",
        "--deps",
        help="Comma-separated dependency ids, e.g. task-a,task-b.",
    ),
    priority: int = typer.Option(0, "--priority", help="Higher value runs first."),
    max_attempts: int = typer.Option(
        3,
        "--max-attempts",
        help="Retry cap before flipping job to blocked.",
    ),
    no_verify: bool = typer.Option(
        False,
        "--no-verify",
        help="Disable verification (skip build -> audit -> fix state machine).",
    ),
) -> None:
    """Insert a new job into the queue."""
    prompt_path = prompt or f"parcels/{job_id}.md"
    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        job = orch.enqueue(
            job_id,
            prompt_path=prompt_path,
            deps=_parse_deps(deps),
            priority=priority,
            max_attempts=max_attempts,
            created_by="cli",
            verification_enabled=not no_verify,
        )
    except Exception as exc:
        console.print(f"[red]enqueue failed:[/red] {exc}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        orch.close()
    console.print(f"[green]enqueued[/green] {job.id} (status={job.status})")


def list_cmd(
    ctx: typer.Context,
    status: str = typer.Option(
        "active",
        "--status",
        help=(
            "Filter: active|all|pending|claimed|running|done|failed|blocked|"
            "landed|merge-blocked. 'active' = pending+claimed+running+blocked (default)."
        ),
    ),
    all_: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Shortcut for --status all.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit structured JSON instead of a Rich table.",
    ),
) -> None:
    """List jobs, defaulting to active (non-terminal) jobs."""
    if all_:
        status = "all"

    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        status_arg: JobStatus | list[JobStatus] | None
        if status == "all":
            status_arg = None
        elif status == "active":
            status_arg = _ACTIVE_STATUSES
        else:
            status_arg = status  # type: ignore[assignment]
        jobs = orch.list_jobs(status=status_arg)
    finally:
        orch.close()

    if json_output:
        typer.echo(
            json.dumps(
                {"filter": status, "jobs": _jobs_to_jsonable(jobs)},
                indent=2,
                default=str,
            )
        )
        return

    if not jobs:
        hint = " (try --all for archive)" if status == "active" else ""
        console.print(f"[dim]no jobs{hint}[/dim]")
        return

    table = Table(title=f"jobs ({status})")
    table.add_column("id")
    table.add_column("status")
    table.add_column("pri")
    table.add_column("att")
    table.add_column("deps")
    table.add_column("worker")
    table.add_column("land")
    table.add_column("commit")
    table.add_column("enqueued")
    for j in jobs:
        land_cell = _render_land_status(j.status, j.merge_block_reason)
        commit_cell = (j.landed_commit or "-")[:10]
        table.add_row(
            j.id,
            j.status,
            str(j.priority),
            f"{j.attempts}/{j.max_attempts}",
            ",".join(j.deps) or "-",
            j.claimed_by or "-",
            land_cell,
            commit_cell,
            j.enqueued_at.isoformat(timespec="seconds") if j.enqueued_at else "-",
        )
    console.print(table)


def status_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Job id to inspect."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Pretty-print JSON. Default emits compact Pydantic JSON.",
    ),
) -> None:
    """Print a single job's state."""
    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        try:
            job = orch.status(job_id)
        except JobNotFound:
            typer.echo(f"no such job: {job_id}", err=True)
            raise typer.Exit(code=4) from None
    finally:
        orch.close()

    if json_output:
        typer.echo(json.dumps(_job_to_jsonable(job), indent=2, default=str))
    else:
        typer.echo(job.model_dump_json())


def requeue_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Job id to requeue."),
) -> None:
    """Reset a failed/blocked job back to pending (resets attempts to 0)."""
    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        try:
            job = orch.requeue(job_id)
        except JobNotFound:
            typer.echo(f"no such job: {job_id}", err=True)
            raise typer.Exit(code=4) from None
    finally:
        orch.close()
    console.print(f"[cyan]requeued[/cyan] {job.id}")


def cancel_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Job id to cancel."),
) -> None:
    """Cancel a job -- marks it failed (terminal)."""
    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        try:
            job = orch.cancel(job_id)
        except JobNotFound:
            typer.echo(f"no such job: {job_id}", err=True)
            raise typer.Exit(code=4) from None
    finally:
        orch.close()
    console.print(f"[yellow]cancelled[/yellow] {job.id}")


def summary_cmd(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit counts as JSON instead of a Rich table.",
    ),
) -> None:
    """Print a status rollup for all jobs."""
    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        s = orch.summary()
    finally:
        orch.close()

    if json_output:
        typer.echo(json.dumps(s, indent=2))
        return

    table = Table(title="queue summary")
    table.add_column("status")
    table.add_column("count", justify="right")
    for k in (
        "pending",
        "claimed",
        "running",
        "done",
        "failed",
        "blocked",
        "auditing",
        "audited",
        "fixing",
        "landing",
        "landed",
        "merge-blocked",
    ):
        table.add_row(k, str(s.get(k, 0)))
    console.print(table)


def reclaim_cmd(ctx: typer.Context) -> None:
    """One-shot reclaim of expired leases (debugging aid).

    Moves jobs with expired leases from claimed back to pending (or blocked
    if they have exhausted max_attempts). Normally the daemon does this
    automatically on each tick.
    """
    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        affected = orch.reclaim_expired()
    finally:
        orch.close()
    console.print(f"reclaimed {len(affected)} job(s): {affected}")


__all__ = [
    "cancel_cmd",
    "enqueue_cmd",
    "list_cmd",
    "reclaim_cmd",
    "requeue_cmd",
    "status_cmd",
    "summary_cmd",
]
