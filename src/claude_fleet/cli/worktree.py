"""Worktree management sub-commands for ``claude-fleet worktree ...``.

worktree list    — list worktrees managed by claude-fleet
worktree retire  — manually retire a worktree (G2 stub for now)
worktree sweep   — sweep orphaned worktrees (G8 stub)
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

console = Console()

worktree_app = typer.Typer(
    name="worktree",
    help="Worktree management commands (list, retire, sweep).",
    no_args_is_help=True,
    add_completion=False,
)


@worktree_app.command("list")
def worktree_list_cmd(ctx: typer.Context) -> None:
    """List worktrees managed by claude-fleet."""
    # TODO(P5 G1): implement — list git worktrees that match known job ids
    db_path = (
        Path(ctx.obj["db"]) if ctx.obj and "db" in ctx.obj else Path("./claude-fleet.db")
    )
    try:
        from claude_fleet.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415
        from claude_fleet.orchestrator.config import OrchestratorConfig  # noqa: PLC0415

        cfg = OrchestratorConfig(db_path=db_path)
        orch = Orchestrator(cfg.db_path, lease_ttl_s=cfg.lease_ttl_s)
        try:
            jobs = orch.list_jobs()
        finally:
            orch.close()
    except Exception as exc:
        typer.echo(f"could not query jobs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    from rich.table import Table  # noqa: PLC0415

    table = Table(title="worktrees")
    table.add_column("job_id")
    table.add_column("status")
    table.add_column("worktree_exists")
    for job in jobs:
        wt = Path(f"worktrees/{job.id}")
        table.add_row(job.id, job.status, "yes" if wt.exists() else "no")
    if not jobs:
        console.print("[dim]no jobs in queue[/dim]")
    else:
        console.print(table)


@worktree_app.command("retire")
def worktree_retire_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Job id whose worktree to retire."),
) -> None:
    """Manually retire (remove) a job's git worktree.

    TODO(P6 G2): implement full worktree auto-retire logic.
    """
    # TODO(P6 G2): implement — call git worktree remove for this job's worktree
    typer.echo(
        f"TODO: worktree retire is not yet implemented (job_id: {job_id}). "
        "Implement in P6 (G2).",
        err=True,
    )
    raise typer.Exit(code=1)


@worktree_app.command("sweep")
def worktree_sweep_cmd(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print orphaned worktrees without removing them.",
    ),
) -> None:
    """Sweep orphaned worktrees (on disk with no corresponding jobs row).

    TODO(P7 G8): implement full sweep logic.
    """
    # TODO(P7 G8): implement — find worktree dirs with no jobs row, remove them
    typer.echo(
        "TODO: worktree sweep is not yet implemented. "
        "Implement in P7 (G8).",
        err=True,
    )
    raise typer.Exit(code=1)


__all__ = ["worktree_app"]
