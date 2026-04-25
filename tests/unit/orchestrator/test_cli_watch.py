"""Tests for the ``watch`` command and its ``_watch_iteration`` core.

Skipped: the ``watch`` command was not lifted into claude-fleet's CLI.
The ``watch`` sub-command polled job state and PID liveness in a loop —
functionality that may be added in a future release as ``claude-fleet watch``
or similar. The ``_watch_iteration`` helper that the tests drove does not
exist in the claude-fleet codebase.
"""

import pytest

pytestmark = pytest.mark.skip(
    reason="TODO P5: 'watch' command not yet implemented in claude-fleet CLI"
)


def test_watch_iteration_reports_rows_for_pending_jobs() -> None:
    ...


def test_watch_iteration_filter_limits_jobs() -> None:
    ...


def test_watch_iteration_reads_pid_and_checks_alive() -> None:
    ...


def test_watch_iteration_detects_dead_worker_but_no_respawn_without_flag() -> None:
    ...


def test_watch_iteration_respawn_reclaims_expired_lease() -> None:
    ...


def test_watch_iteration_respawn_skips_jobs_with_parcel_done() -> None:
    ...


def test_watch_iteration_reports_claimed_age() -> None:
    ...


def test_watch_cli_iterations_bound_exits_cleanly() -> None:
    ...


def test_watch_cli_json_emits_iteration_object() -> None:
    ...
