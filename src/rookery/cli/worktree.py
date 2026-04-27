"""Worktree management sub-commands for ``rookery worktree ...``.

worktree list    — list worktrees managed by rookery
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
    """List worktrees managed by rookery."""
    # TODO(P5 G1): implement — list git worktrees that match known job ids
    db_path = (
        Path(ctx.obj["db"]) if ctx.obj and "db" in ctx.obj else Path("./rookery.db")
    )
    try:
        from rookery.orchestrator.config import OrchestratorConfig  # noqa: PLC0415
        from rookery.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415

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
        Path(ctx.obj["db"]) if ctx.obj and "db" in ctx.obj else Path("./rookery.db")
    )

    # Step 1: look up the job and validate it is landed.
    try:
        from rookery.orchestrator.config import OrchestratorConfig  # noqa: PLC0415
        from rookery.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415

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
        from rookery.worktree import GitWorktreeLifecycle  # noqa: PLC0415

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
    worktree_base: Path = typer.Option(
        None,
        "--worktree-base",
        help=(
            "Root directory holding per-job worktree subdirectories. "
            "Defaults to the value from OrchestratorConfig (./worktrees)."
        ),
    ),
    repo_root: Path | None = typer.Option(
        None,
        "--repo-root",
        help=(
            "Root of the git repository that owns the worktrees. "
            "Defaults to the current working directory."
        ),
    ),
    orphan_age_hours: float | None = typer.Option(
        None,
        "--orphan-age-hours",
        help=(
            "Minimum age in hours before a terminal-status worktree is swept. "
            "Defaults to the value from OrchestratorConfig (168 h = 7 days)."
        ),
    ),
) -> None:
    """Sweep orphaned worktrees (on disk with no corresponding jobs row).

    An orphan is a worktree directory where:
    - No jobs row exists for that directory name, OR
    - The jobs row is in a terminal state (failed/blocked/landed) AND the
      worktree is older than --orphan-age-hours.

    With --dry-run, lists candidates without removing anything.
    """
    import asyncio  # noqa: PLC0415

    db_path = (
        Path(ctx.obj["db"]) if ctx.obj and "db" in ctx.obj else Path("./rookery.db")
    )

    from rookery.orchestrator.config import OrchestratorConfig  # noqa: PLC0415

    cfg = OrchestratorConfig(db_path=db_path)

    resolved_worktree_base: Path = worktree_base if worktree_base is not None else cfg.worktree_base
    resolved_age_hours: float = orphan_age_hours if orphan_age_hours is not None else cfg.orphan_age_hours

    from rookery.worktree import GitWorktreeLifecycle, find_orphans  # noqa: PLC0415

    async def _run() -> int:
        """Return count of orphans found (and removed if not dry_run)."""
        orphans = await find_orphans(
            worktree_base=resolved_worktree_base,
            db_path=db_path,
            orphan_age_hours=resolved_age_hours,
        )

        if not orphans:
            console.print("[dim]no orphaned worktrees found[/dim]")
            return 0

        from rich.table import Table  # noqa: PLC0415

        table = Table(title="orphaned worktrees", show_lines=False)
        table.add_column("path", style="bold")
        table.add_column("reason")
        table.add_column("last modified")
        for orphan in orphans:
            table.add_row(
                str(orphan.path),
                orphan.reason,
                orphan.last_modified.strftime("%Y-%m-%d %H:%M UTC"),
            )
        console.print(table)

        if dry_run:
            console.print(
                f"[yellow]found {len(orphans)} orphan(s) (dry-run — not removed)[/yellow]"
            )
            return len(orphans)

        # Remove each orphan using force_remove.
        lifecycle = GitWorktreeLifecycle(
            base_dir=resolved_worktree_base,
            branch_prefix="parcel/",
            base_branch="main",
            repo_root=repo_root,
        )
        removed = 0
        for orphan in orphans:
            try:
                await lifecycle.force_remove(orphan.path)
                console.print(f"[green]removed[/green] {orphan.path}")
                removed += 1
            except Exception as exc:
                console.print(
                    f"[red]failed[/red] to remove {orphan.path}: {exc}",
                    highlight=False,
                )

        console.print(f"removed {removed} of {len(orphans)} worktree(s)")
        return removed

    try:
        asyncio.run(_run())
    except Exception as exc:
        typer.echo(f"error: sweep failed: {exc}", err=True)
        raise typer.Exit(code=1) from None


__all__ = ["worktree_app"]
