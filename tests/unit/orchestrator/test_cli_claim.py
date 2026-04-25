"""Tests for the ``claim`` command — status / tail / done dump.

Skipped: the ``claim`` command was not lifted into claude-fleet's CLI.
The ``claim`` sub-command inspected a running worker's worktree and PID state
— functionality that maps to a future ``claude-fleet status --watch`` or
similar surface. The underlying helper functions (_find_parcel_done,
_tail_lines, _read_worktree_pid) do exist in claude_fleet.cli.daemon but
there is no top-level ``claim`` Typer command.
"""

import pytest

pytestmark = pytest.mark.skip(
    reason="TODO P5: 'claim' command not yet implemented in claude-fleet CLI"
)


def test_claim_status_on_missing_job_and_missing_worktree() -> None:
    ...


def test_claim_status_with_enqueued_job_and_pid_alive() -> None:
    ...


def test_claim_status_dead_worker_reported() -> None:
    ...


def test_claim_status_done_state() -> None:
    ...


def test_claim_json_matches_expected_schema() -> None:
    ...


def test_claim_done_flag_dumps_parcel_done_file() -> None:
    ...


def test_claim_done_flag_errors_when_no_parcel_done() -> None:
    ...


def test_claim_tail_shows_last_lines() -> None:
    ...


def test_claim_tail_errors_when_no_log() -> None:
    ...


def test_find_parcel_done_prefers_new_convention() -> None:
    ...


def test_find_parcel_done_legacy_fallback() -> None:
    ...


def test_tail_lines_returns_last_n() -> None:
    ...


def test_tail_lines_missing_file() -> None:
    ...
