"""Parcel management sub-commands for ``claude-fleet parcel ...``.

parcel new      — scaffold a parcel template (G3 stub)
parcel validate — validate a parcel file (G3 stub)
parcel build    — alias for enqueue (spec → queue)
"""

from __future__ import annotations

from pathlib import Path

import typer

from claude_fleet.cli.queue import enqueue_cmd

parcel_app = typer.Typer(
    name="parcel",
    help="Parcel lifecycle commands (new, validate, build).",
    no_args_is_help=True,
    add_completion=False,
)


@parcel_app.command("new")
def parcel_new_cmd(
    ctx: typer.Context,
    parcel_id: str = typer.Argument(..., help="Parcel id, e.g. add-oauth-flow"),
    prompt: str = typer.Option(
        "",
        "--prompt",
        help="Output path for the generated parcel file. Defaults to parcels/<id>.md.",
    ),
) -> None:
    """Scaffold a parcel template file at parcels/<id>.md.

    TODO(P5 G3): implement parcel scaffolding.
    """
    # TODO(P5 G3): implement — create parcels/<id>.md with default frontmatter
    out_path = prompt or f"parcels/{parcel_id}.md"
    typer.echo(
        f"TODO: parcel new is not yet implemented (target: {out_path}). "
        "Implement in P5 (G3).",
        err=True,
    )
    raise typer.Exit(code=1)


@parcel_app.command("validate")
def parcel_validate_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Path to the parcel file to validate."),
) -> None:
    """Validate a parcel file's frontmatter and structure.

    TODO(P5 G3): implement parcel validation.
    """
    # TODO(P5 G3): implement — parse frontmatter, check required fields
    typer.echo(
        f"TODO: parcel validate is not yet implemented (path: {path}). "
        "Implement in P5 (G3).",
        err=True,
    )
    raise typer.Exit(code=1)


@parcel_app.command("build")
def parcel_build_cmd(
    ctx: typer.Context,
    spec_path: str = typer.Argument(..., help="Path to parcel spec file."),
    deps: str = typer.Option(
        "",
        "--deps",
        help="Comma-separated dependency ids.",
    ),
    priority: int = typer.Option(0, "--priority", help="Higher value runs first."),
    max_attempts: int = typer.Option(3, "--max-attempts", help="Retry cap."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Disable verification."),
) -> None:
    """Build (enqueue) a parcel from a spec file.

    Currently a direct alias for ``enqueue`` — reserved for future
    spec-to-parcel transformations (e.g. spec → LLM-generated parcel).
    The spec file name (without extension) is used as the job id.
    """
    job_id = Path(spec_path).stem
    # Delegate to enqueue_cmd by reconstructing the call
    from claude_fleet.cli.queue import _db_from_ctx, _open_orch, _parse_deps  # noqa: PLC0415

    db_path = _db_from_ctx(ctx)
    orch = _open_orch(db_path)
    try:
        job = orch.enqueue(
            job_id,
            prompt_path=spec_path,
            deps=_parse_deps(deps),
            priority=priority,
            max_attempts=max_attempts,
            created_by="cli:parcel-build",
            verification_enabled=not no_verify,
        )
    except Exception as exc:
        typer.echo(f"parcel build failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        orch.close()
    from rich.console import Console  # noqa: PLC0415

    Console().print(f"[green]enqueued[/green] {job.id} (status={job.status})")


__all__ = ["parcel_app"]
