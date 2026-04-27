"""JsonResultAdapter — verdict from ``result.json`` at worktree root (G4).

Expected ``result.json`` shape::

    {
        "verdict": "PASS",          // required: PASS | PASS_WITH_WARNINGS | BLOCK | UNKNOWN
        "summary": "...",           // optional string
        "detail": {...}             // optional object, forwarded verbatim
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from rookery.adapters.base import VerdictAdapter, VerdictResult
from rookery.orchestrator.backend import AuditVerdict

_VALID_VERDICTS = frozenset({"PASS", "PASS_WITH_WARNINGS", "BLOCK", "UNKNOWN"})

_RESULT_FILENAME = "result.json"


class JsonResultAdapter(VerdictAdapter):
    """Detect completion via ``<worktree>/result.json``.

    Returns ``None`` when the file does not exist (worker still running).
    Returns ``VerdictResult(verdict='UNKNOWN')`` when the file exists but is
    malformed, missing the required ``verdict`` key, or contains an
    unrecognised verdict value.
    """

    def detect(self, worktree: Path, job_id: str) -> VerdictResult | None:  # noqa: ARG002
        result_file = worktree / _RESULT_FILENAME
        if not result_file.exists():
            return None  # worker not done yet

        try:
            raw = result_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            return VerdictResult(
                verdict="UNKNOWN",
                detail={
                    "result_file": str(result_file),
                    "error": f"unreadable or invalid JSON: {exc}",
                },
            )

        if not isinstance(data, dict):
            return VerdictResult(
                verdict="UNKNOWN",
                detail={
                    "result_file": str(result_file),
                    "error": "result.json must be a JSON object",
                },
            )

        verdict_raw = data.get("verdict")
        if not isinstance(verdict_raw, str) or verdict_raw not in _VALID_VERDICTS:
            return VerdictResult(
                verdict="UNKNOWN",
                detail={
                    "result_file": str(result_file),
                    "error": (
                        f"missing or invalid 'verdict' key: {verdict_raw!r} — "
                        f"expected one of {sorted(_VALID_VERDICTS)}"
                    ),
                },
            )

        summary_raw = data.get("summary")
        summary: str | None = summary_raw if isinstance(summary_raw, str) else None

        detail_raw = data.get("detail", {})
        detail: dict[str, object] = (
            dict(detail_raw) if isinstance(detail_raw, dict) else {}
        )
        detail["result_file"] = str(result_file)

        return VerdictResult(
            verdict=cast(AuditVerdict, verdict_raw),
            summary=summary,
            detail=detail,
        )


__all__ = ["JsonResultAdapter"]
