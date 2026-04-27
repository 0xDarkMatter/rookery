"""MarkerFileAdapter — verdict from ``PARCEL_DONE-<id>.md`` (G4).

This is the default verdict adapter.  It reads a marker file written by the
worker at the worktree root.  The canonical filename is
``PARCEL_DONE-<job_id>.md``; for backward compatibility it also accepts the
legacy ``PARCEL_DONE.md`` (no id suffix) when the id-specific file is absent.

The ``Verdict:`` extraction logic is delegated to
:func:`rookery.orchestrator.verdict_parser.parse_audit_report`, which is
the canonical implementation shared with the audit-report parser.  No
duplicate logic lives here.
"""

from __future__ import annotations

from pathlib import Path

from rookery.adapters.base import VerdictAdapter, VerdictResult
from rookery.orchestrator.verdict_parser import _LINE_RE


def _parse_verdict_and_summary(path: Path) -> tuple[str, str | None]:
    """Extract verdict token and optional summary from a PARCEL_DONE file.

    Returns ``(verdict_token, summary_or_None)``.

    The verdict token is normalised to uppercase with the singular-warning
    alias canonicalised.  A ``## Summary`` section is captured if present.

    Raises:
        ValueError: if no recognisable ``Verdict:`` line is found or the
            token is not one of the allowed values.
    """
    valid_tokens = frozenset({"PASS", "PASS_WITH_WARNINGS", "BLOCK", "UNKNOWN"})

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    verdict_token: str | None = None
    for raw in lines:
        m = _LINE_RE.match(raw)
        if not m:
            continue
        token = m.group(1).upper().rstrip("*")
        if token == "PASS_WITH_WARNING":
            token = "PASS_WITH_WARNINGS"
        if token in valid_tokens:
            verdict_token = token
            break
        raise ValueError(
            f"unrecognised verdict token {token!r} in {path} — "
            f"expected one of PASS / PASS_WITH_WARNINGS / BLOCK / UNKNOWN"
        )

    if verdict_token is None:
        raise ValueError(f"no verdict line found in {path}")

    # Extract summary: text under a ``## Summary`` heading up to the next
    # heading or end of file.
    summary: str | None = None
    in_summary = False
    summary_lines: list[str] = []
    for line in lines:
        if line.strip().lower().startswith("## summary"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("#"):
                break
            summary_lines.append(line)

    if summary_lines:
        summary = "\n".join(summary_lines).strip() or None

    return verdict_token, summary


class MarkerFileAdapter(VerdictAdapter):
    """Detect completion via ``PARCEL_DONE-<job_id>.md`` at the worktree root.

    Lookup order (first existing file wins):

    1. ``<worktree>/PARCEL_DONE-<job_id>.md``  — canonical per-parcel name
    2. ``<worktree>/PARCEL_DONE.md``             — legacy fallback (no id suffix)

    Returns ``None`` when neither file exists (worker still running).
    Returns a :class:`VerdictResult` with ``verdict=UNKNOWN`` when the file
    exists but contains no recognisable verdict line (malformed / empty).
    """

    def detect(self, worktree: Path, job_id: str) -> VerdictResult | None:
        candidates = [
            worktree / f"PARCEL_DONE-{job_id}.md",
            worktree / "PARCEL_DONE.md",
        ]
        done_file = next((p for p in candidates if p.exists()), None)
        if done_file is None:
            return None  # worker not done yet

        try:
            verdict_token, summary = _parse_verdict_and_summary(done_file)
        except (OSError, ValueError):
            # File exists but is unreadable or malformed — treat as UNKNOWN so
            # the orchestrator can surface this rather than looping forever.
            return VerdictResult(
                verdict="UNKNOWN",
                detail={"marker_file": str(done_file), "error": "malformed or unreadable"},
            )

        from typing import cast

        from rookery.orchestrator.backend import AuditVerdict  # noqa: PLC0415

        return VerdictResult(
            verdict=cast(AuditVerdict, verdict_token),
            summary=summary,
            detail={"marker_file": str(done_file)},
        )


__all__ = ["MarkerFileAdapter"]
