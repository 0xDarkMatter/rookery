"""Unit tests for ``_read_worktree_pid``.

Skipped: ``_read_worktree_pid`` was not lifted into the rookery CLI.
The function reads ``.pid`` / ``.winpid`` from a worker worktree to determine
a running worker's PID. This functionality may be added in a future release
as part of a ``rookery watch`` or ``rookery claim`` command.
"""

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "TODO P5: _read_worktree_pid not in rookery CLI; "
        "re-enable when 'claim'/'watch' commands are implemented"
    )
)


def test_prefers_winpid_on_windows() -> None:
    ...


def test_falls_back_to_pid_when_winpid_absent_on_windows() -> None:
    ...


def test_falls_back_to_pid_when_winpid_malformed_on_windows() -> None:
    ...


def test_uses_pid_only_on_posix() -> None:
    ...


def test_returns_none_when_both_absent() -> None:
    ...


def test_returns_none_when_pid_malformed_and_no_winpid() -> None:
    ...


def test_returns_none_when_worktree_missing() -> None:
    ...
