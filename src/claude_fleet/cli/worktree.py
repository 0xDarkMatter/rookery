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
    worktree_base: Path = typer.Option(
        Path("./worktrees"),
        "--worktree-base",
        help="Root directory holding per-job worktree subdirectories.",
    ),
    repo_root: Path | None = typer.Option(
        None,
        "--repo-root",
        help=(
            "Root of the git repository that owns the worktrees. "
            "Defaults to the current working directory."
        ),
    ),
) -> None:
    """Manually retire (remove) a job's git worktree.

    The job must be in the ``landed`` state. Exits with code 7 on
    worktree operation failure, code 1 on validation errors.
    """
    import asyncio  # noqa: PLC0415

    db_path = (
        Path(ctx.obj["db"]) if ctx.obj and "db" in ctx.obj else Path("./claude-fleet.db")
    )

    # Step 1: look up the job and validate it is landed.
    try:
        from claude_fleet.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415
        from claude_fleet.orchestrator.config import OrchestratorConfig  # noqa: PLC0415

        cfg = OrchestratorConfig(db_path=db_path)
        orch = Orchestrator(cfg.db_path, lease_ttl_s=cfg.lease_ttl_s)
        try:
            job = orch.status(job_id)
        finally:
            orch.close()
    except Exception as exc:
        typer.echo(f"error: could not query job {job_id!r}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if job.status != "landed":
        typer.echo(
            f"error: job {job_id!r} is not landed (status={job.status!r}). "
            "Only landed jobs may be retired.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Step 2: resolve the worktree path.
    wt_path = worktree_base / job_id

    # Step 3: call lifecycle retire.
    try:
        from claude_fleet.worktree import GitWorktreeLifecycle  # noqa: PLC0415

        lifecycle = GitWorktreeLifecycle(
            base_dir=worktree_base,
            branch_prefix="parcel/",
            base_branch="main",
            repo_root=repo_root,
        )

        async def _run() -> None:
            await lifecycle.retire(job, wt_path)

        asyncio.run(_run())
    except ValueError as exc:
        # Non-landed status (belt-and-suspenders after the status check above).
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"error: worktree retire failed for {job_id!r}: {exc}", err=True)
        raise typer.Exit(code=7) from None

    console.print(f"[green]retired[/green] worktree for job [bold]{job_id}[/bold]")


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
