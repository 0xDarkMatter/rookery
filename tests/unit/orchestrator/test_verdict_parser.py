"""Unit tests for :mod:`rookery.orchestrator.verdict_parser`."""

from __future__ import annotations

from pathlib import Path

import pytest

from rookery.orchestrator.verdict_parser import parse_audit_report


def _write(tmp_path: Path, body: str) -> Path:
    report = tmp_path / "audit.md"
    report.write_text(body, encoding="utf-8")
    return report


def test_pass_bold_markdown(tmp_path: Path) -> None:
    path = _write(tmp_path, "# Audit: X\n\n**Verdict:** PASS\n\n**Summary:** ok\n")
    assert parse_audit_report(path) == "PASS"


def test_pass_with_warnings_bold(tmp_path: Path) -> None:
    path = _write(tmp_path, "**Verdict:** PASS_WITH_WARNINGS\n")
    assert parse_audit_report(path) == "PASS_WITH_WARNINGS"


def test_block_bold(tmp_path: Path) -> None:
    path = _write(tmp_path, "**Verdict:** BLOCK\nother text\n")
    assert parse_audit_report(path) == "BLOCK"


def test_plain_verdict_line(tmp_path: Path) -> None:
    # Forward-compat form from the R2-5 spec ("VERDICT: ...").
    path = _write(tmp_path, "VERDICT: PASS\n")
    assert parse_audit_report(path) == "PASS"


def test_lowercase_verdict_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path, "**verdict:** block\n")
    assert parse_audit_report(path) == "BLOCK"


def test_singular_warning_normalized(tmp_path: Path) -> None:
    path = _write(tmp_path, "**Verdict:** PASS_WITH_WARNING\n")
    assert parse_audit_report(path) == "PASS_WITH_WARNINGS"


def test_first_verdict_wins(tmp_path: Path) -> None:
    # A re-audit may quote the prior verdict later in the prose; only the
    # first line (top of the report) is authoritative.
    body = (
        "**Verdict:** BLOCK\n\n"
        "## Phase D — Quality\n\n"
        "prior pass, but: **Verdict:** PASS (from iter-1 quoted here)\n"
    )
    path = _write(tmp_path, body)
    assert parse_audit_report(path) == "BLOCK"


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_audit_report(tmp_path / "does-not-exist.md")


def test_no_verdict_line_raises_value_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "# Audit: X\n\nno verdict in this file\n")
    with pytest.raises(ValueError, match="no verdict line"):
        parse_audit_report(path)


def test_garbage_verdict_token_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "**Verdict:** MAYBE\n")
    with pytest.raises(ValueError, match="unrecognised verdict"):
        parse_audit_report(path)


def test_verdict_with_trailing_asterisks(tmp_path: Path) -> None:
    # Some operators bold the verdict value too: ``**Verdict:** **PASS**``.
    path = _write(tmp_path, "**Verdict:** **PASS**\n")
    assert parse_audit_report(path) == "PASS"
