"""Parcel management sub-commands for ``rookery parcel ...``.

parcel new      — scaffold a parcel template (G3)
parcel validate — validate a parcel file (G3)
parcel build    — alias for enqueue (spec → queue)
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from rookery.cli.queue import enqueue_cmd
from rookery.parcel import parcel_new, parcel_validate

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
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing parcel file.",
    ),
) -> None:
    """Scaffold a parcel template file at parcels/<id>.md."""
    prompt_path = Path(prompt) if prompt else None
    try:
        target = parcel_new(parcel_id, prompt_path=prompt_path, force=force)
    except FileExistsError as exc:
        Console(stderr=True).print(
            f"[red]error:[/red] file already exists: {exc}. Use --force to overwrite."
        )
        raise typer.Exit(code=1) from None
    Console().print(f"[green]created[/green] {target}")


@parcel_app.command("validate")
def parcel_validate_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Path to the parcel file to validate."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit structured JSON instead of human-readable output.",
    ),
) -> None:
    """Validate a parcel file's frontmatter and structure."""
    result = parcel_validate(Path(path))

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "ok": result.ok,
                    "errors": result.errors,
                    "warnings": result.warnings,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=0 if result.ok else 1)

    console = Console()
    console_err = Console(stderr=True)

    for err in result.errors:
        console_err.print(f"[red]error:[/red] {err}")
    for warn in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warn}")

    if result.ok:
        console.print(f"[green]ok[/green] {path}")
    else:
        console_err.print(f"[red]invalid[/red] {path} ({len(result.errors)} error(s))")

    raise typer.Exit(code=0 if result.ok else 1)


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
    from rookery.cli.queue import _db_from_ctx, _open_orch, _parse_deps  # noqa: PLC0415

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
