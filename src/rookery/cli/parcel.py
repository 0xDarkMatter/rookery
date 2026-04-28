"""Parcel management sub-commands for ``rookery parcel ...``.

parcel new      — scaffold a parcel template (G3)
parcel validate — validate a parcel file (G3)
parcel build    — alias for enqueue (spec → queue)
parcel done     — worker reports terminal verdict to the queue DB (v0.3)
parcel progress — worker reports a streaming progress event (v0.3)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console

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


# ---------------------------------------------------------------------------
# v0.3 worker reporting protocol
# ---------------------------------------------------------------------------
#
# Workers invoke ``rookery parcel done`` / ``rookery parcel progress`` from
# inside the parcel worktree.  The daemon injects three env vars when it
# spawns the worker, which these commands read to find the queue DB:
#
#   ROOKERY_DB              — absolute path to the queue's SQLite db
#   ROOKERY_PARCEL_ID       — the parcel id (== jobs.id)
#   ROOKERY_PARCEL_ATTEMPT  — current attempt number (1-indexed)
#
# The legacy marker-file protocol (PARCEL_DONE-<id>.md) still works as a
# fallback for parcels that pre-date this CLI.

_VERDICT_CHOICES = ["PASS", "PASS_WITH_WARNINGS", "BLOCK", "UNKNOWN"]


def _resolve_worker_context() -> tuple[Path, str, int]:
    """Resolve the (db_path, parcel_id, attempt) tuple from worker env vars.

    Raises ``typer.Exit(2)`` with a clear remediation message when any of
    the three are missing — usually means the user invoked the helper by
    hand outside a daemon-spawned worker.
    """
    db = os.environ.get("ROOKERY_DB")
    pid = os.environ.get("ROOKERY_PARCEL_ID")
    attempt = os.environ.get("ROOKERY_PARCEL_ATTEMPT")
    missing = [
        name
        for name, val in (
            ("ROOKERY_DB", db),
            ("ROOKERY_PARCEL_ID", pid),
            ("ROOKERY_PARCEL_ATTEMPT", attempt),
        )
        if not val
    ]
    if missing:
        Console(stderr=True).print(
            f"[red]error:[/red] missing env var(s): {', '.join(missing)}.\n\n"
            "  rookery parcel done / progress is normally invoked by a\n"
            "  worker spawned by the daemon, which sets ROOKERY_DB,\n"
            "  ROOKERY_PARCEL_ID, and ROOKERY_PARCEL_ATTEMPT automatically.\n\n"
            "  To run by hand for testing, set them explicitly:\n"
            "    export ROOKERY_DB=/abs/path/to/rookery.db\n"
            "    export ROOKERY_PARCEL_ID=<parcel-id>\n"
            "    export ROOKERY_PARCEL_ATTEMPT=1"
        )
        raise typer.Exit(code=2)
    assert db is not None and pid is not None and attempt is not None
    try:
        attempt_i = int(attempt)
    except ValueError:
        Console(stderr=True).print(
            f"[red]error:[/red] ROOKERY_PARCEL_ATTEMPT must be int, got {attempt!r}"
        )
        raise typer.Exit(code=2) from None
    return Path(db), pid, attempt_i


@parcel_app.command("done")
def parcel_done_cmd(
    verdict: str = typer.Option(
        ...,
        "--verdict",
        case_sensitive=False,
        help=f"Terminal verdict: one of {', '.join(_VERDICT_CHOICES)}.",
    ),
    summary: str = typer.Option(
        ...,
        "--summary",
        help="One-line headline (shows in `rookery list` and auto-commit messages).",
    ),
    detail_file: str = typer.Option(
        "",
        "--detail-file",
        help="Path to a markdown file with longer-form detail. Read into detail_md.",
    ),
    detail: str = typer.Option(
        "",
        "--detail",
        help="Inline markdown detail (alternative to --detail-file).",
    ),
    tokens_in: int = typer.Option(0, "--tokens-in", help="Worker-reported input tokens."),
    tokens_out: int = typer.Option(0, "--tokens-out", help="Worker-reported output tokens."),
    duration_s: float = typer.Option(0.0, "--duration-s", help="Wall-clock seconds."),
    tests_passed: int = typer.Option(-1, "--tests-passed", help="Test count (omit if N/A)."),
    tests_failed: int = typer.Option(-1, "--tests-failed", help="Test count (omit if N/A)."),
    files_changed: int = typer.Option(-1, "--files-changed", help="File count (omit if N/A)."),
    write_marker_file: bool = typer.Option(
        False,
        "--write-marker-file",
        help=(
            "Also write a legacy PARCEL_DONE-<id>.md in the worktree for audit. "
            "Default off — the DB row is the source of truth."
        ),
    ),
) -> None:
    """Report a terminal verdict to the queue DB.

    Run from inside the parcel worktree at the end of the parcel script. The
    daemon picks up the verdict on its next harvest tick (~5s) and transitions
    the job to ``done`` (PASS / PASS_WITH_WARNINGS) or ``failed`` (BLOCK).

    Example::

        rookery parcel done --verdict PASS \\
            --summary "OAuth flow added with refresh-token support" \\
            --tokens-in 12500 --tokens-out 3200 --duration-s 187 \\
            --tests-passed 42 --tests-failed 0 --files-changed 3
    """
    verdict_norm = verdict.upper()
    if verdict_norm not in _VERDICT_CHOICES:
        Console(stderr=True).print(
            f"[red]error:[/red] verdict must be one of {_VERDICT_CHOICES}, got {verdict!r}"
        )
        raise typer.Exit(code=2)

    db_path, parcel_id, attempt = _resolve_worker_context()

    detail_md: str | None = None
    if detail_file and detail:
        Console(stderr=True).print("[red]error:[/red] use --detail-file OR --detail, not both")
        raise typer.Exit(code=2)
    if detail_file:
        try:
            detail_md = Path(detail_file).read_text(encoding="utf-8")
        except OSError as exc:
            Console(stderr=True).print(f"[red]error:[/red] failed to read --detail-file: {exc}")
            raise typer.Exit(code=2) from None
    elif detail:
        detail_md = detail

    # If detail wasn't supplied via flag, also read from stdin when piped.
    if detail_md is None and not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            detail_md = piped

    # Lazy import — avoids loading the Orchestrator + sqlite3 on --help.
    from rookery.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415

    orch = Orchestrator(db_path, lease_ttl_s=60)
    try:
        orch.write_parcel_result(
            parcel_id,
            attempt=attempt,
            verdict=verdict_norm,
            summary=summary,
            reported_via="cli",
            detail_md=detail_md,
            tokens_in=tokens_in if tokens_in > 0 else None,
            tokens_out=tokens_out if tokens_out > 0 else None,
            duration_s=duration_s if duration_s > 0 else None,
            tests_passed=tests_passed if tests_passed >= 0 else None,
            tests_failed=tests_failed if tests_failed >= 0 else None,
            files_changed=files_changed if files_changed >= 0 else None,
        )
    finally:
        orch.close()

    if write_marker_file:
        worktree = os.environ.get("ROOKERY_WORKTREE")
        if worktree:
            marker = Path(worktree) / f"PARCEL_DONE-{parcel_id}.md"
            body = f"Verdict: {verdict_norm}\n\n## Summary\n\n{summary}\n"
            if detail_md:
                body += f"\n## Detail\n\n{detail_md}\n"
            marker.write_text(body, encoding="utf-8")

    Console().print(
        f"[green]verdict {verdict_norm}[/green] recorded for "
        f"parcel [cyan]{parcel_id}[/cyan] (attempt {attempt})"
    )


@parcel_app.command("progress")
def parcel_progress_cmd(
    label: str = typer.Argument(..., help="Short progress label, e.g. 'Implemented OAuth'."),
    detail: str = typer.Option(
        "",
        "--detail",
        help="Optional longer text describing this progress step.",
    ),
    phase: str = typer.Option(
        "running",
        "--phase",
        help="One of running | stuck | complete (informational).",
    ),
) -> None:
    """Append a streaming progress event to the parcel's event log.

    Daemon never gates state transitions on these — they're for live
    observability via ``rookery logs --events`` and the future
    ``rookery watch`` TUI.

    Example::

        rookery parcel progress "Implemented OAuth model"
        rookery parcel progress "Wired up middleware" --detail "see src/auth/mw.py"
    """
    db_path, parcel_id, attempt = _resolve_worker_context()

    from rookery.orchestrator.orchestrator import Orchestrator  # noqa: PLC0415

    orch = Orchestrator(db_path, lease_ttl_s=60)
    try:
        eid = orch.append_parcel_event(
            parcel_id,
            attempt=attempt,
            event_type="progress",
            label=label,
            detail=detail or None,
            payload={"phase": phase} if phase != "running" else None,
        )
    finally:
        orch.close()

    Console().print(
        f"[dim]event #{eid}[/dim] [cyan]{parcel_id}[/cyan] {phase}: {label}"
    )


__all__ = ["parcel_app"]
