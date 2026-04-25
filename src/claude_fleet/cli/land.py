"""Land commands for ``claude-fleet``.

land             — manually land a PASS'd job (when auto_land=false)
land-history     — show land_events rows for a job
land retry       — retry a merge-blocked job (G9 stub)
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from claude_fleet.orchestrator.orchestrator import JobNotFound, LandRetryError, Orchestrator

console = Console()

land_app = typer.Typer(
    name="land",
    help="Land commands (manual land, history, retry).",
    no_args_is_help=True,
    add_completion=False,
)


def _db_from_ctx(ctx: typer.Context) -> Path:
    if ctx.obj and "db" in ctx.obj:
        return Path(ctx.obj["db"])
    return Path("./claude-fleet.db")


def _open_orch(db_path: Path) -> Orchestrator:
    from claude_fleet.orchestrator.config import OrchestratorConfig  # noqa: PLC0415

    cfg = OrchestratorConfig(db_path=db_path)
    return Orchestrator(cfg.db_path, lease_ttl_s=cfg.lease_ttl_s)


def land_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Job id to land."),
) -> None:
    """Manually trigger landing for a PASS'd job.

    Use when auto_land=false or to recover a merge-blocked job.
    Requires the job to be in 'done' status with a PASS audit verdict.
    """
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

    # Landing implementation requires the LandBackend (P5+).
    # For now, surface what we know about the job and explain.
    console.print(
        f"[yellow]land[/yellow] {job_id} status={job.status} "
        f"verdict={job.audit_verdict}"
    )
    console.print(
        "[dim]Automatic landing requires the daemon with auto_land=true. "
        "Manual land implementation: TODO(P6 G4).[/dim]"
    )
    raise typer.Exit(code=1)


def land_history_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Job id to show land history for."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit structured JSON instead of a table.",
    ),
) -> None:
    """Show land_events rows recorded for a job."""
    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        try:
            events = orch.land_history(job_id)
        except JobNotFound:
            typer.echo(f"no such job: {job_id}", err=True)
            raise typer.Exit(code=4) from None
    finally:
        orch.close()

    if json_output:
        data = [json.loads(e.model_dump_json()) for e in events]
        typer.echo(json.dumps(data, indent=2, default=str))
        return

    if not events:
        console.print(f"[dim]no land attempts recorded for {job_id}[/dim]")
        return

    table = Table(title=f"land history: {job_id}")
    table.add_column("attempt")
    table.add_column("phase")
    table.add_column("outcome")
    table.add_column("commit")
    table.add_column("detail")
    table.add_column("at")
    for e in events:
        table.add_row(
            str(e.attempt),
            e.phase,
            e.outcome,
            (e.commit_sha or "-")[:10],
            (e.detail or "-")[:60],
            e.created_at.isoformat(timespec="seconds") if e.created_at else "-",
        )
    console.print(table)


@land_app.command("retry")
def land_retry_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Job id to retry landing for."),
    worktree_base: Path | None = typer.Option(
        None,
        "--worktree-base",
        help="Root directory of per-job worktrees. When supplied, the worktree "
        "must exist on disk or the command exits with code 7.",
    ),
) -> None:
    """Retry landing for a merge-blocked job.

    Resets merge_block_reason, increments land_attempts, and re-enters the
    land flow. The daemon will pick up the job on the next tick.
    """
    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        try:
            orch.retry_land(job_id, worktree_base=worktree_base)
        except JobNotFound:
            typer.echo(f"no such job: {job_id}", err=True)
            raise typer.Exit(code=4) from None
        except LandRetryError as exc:
            msg = str(exc)
            typer.echo(msg, err=True)
            # "worktree missing" → exit 7 (worktree op failed per API.md)
            if "worktree missing" in msg or "re-enqueue" in msg:
                raise typer.Exit(code=7) from None
            raise typer.Exit(code=1) from None
    finally:
        orch.close()

    typer.echo(f"land retry queued for {job_id}")


# Wire land and land-history as direct sub-commands on land_app
land_app.command("history")(land_history_cmd)


__all__ = ["land_app", "land_cmd", "land_history_cmd"]
