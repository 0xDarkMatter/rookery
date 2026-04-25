"""Unit tests for ``claude_fleet.profile_selector`` (G5).

Coverage
--------
- EnvVarSelector: empty list → ProfileListEmpty
- EnvVarSelector: comma-list → round-robins (4 picks over 2 profiles)
- EnvVarSelector: reads CLAUDE_FLEET_PROFILES env var when no list given
- ClaudeLbSelector: mocked subprocess → parses JSON output into ProfileInfo
- ClaudeLbSelector: binary missing → ClaudeLbBinaryMissing
- ClaudeLbSelector: subprocess non-zero exit → RuntimeError with stderr
- ClaudeLbSelector: malformed JSON → RuntimeError
- WorkerBackend: CLAUDE_FLEET_PROFILES env → auto-wires EnvVarSelector
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_fleet.profile_selector import (
    ClaudeLbBinaryMissing,
    ClaudeLbSelector,
    EnvVarSelector,
    ProfileInfo,
    ProfileListEmpty,
    ProfileSelector,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_proc(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> MagicMock:
    """Build a fake asyncio.subprocess.Process for mocking create_subprocess_exec."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _lb_json(name: str) -> bytes:
    """Build minimal claude-lb pick --json response bytes."""
    return json.dumps({"data": {"name": name}}).encode()


# ---------------------------------------------------------------------------
# ProfileSelector ABC
# ---------------------------------------------------------------------------


def test_profile_selector_is_abstract() -> None:
    """ProfileSelector cannot be instantiated directly."""
    with pytest.raises(TypeError):
        ProfileSelector()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# EnvVarSelector — static list
# ---------------------------------------------------------------------------


async def test_env_var_selector_empty_list_raises() -> None:
    """EnvVarSelector with an empty profile list raises ProfileListEmpty on pick()."""
    sel = EnvVarSelector(profiles=[])
    with pytest.raises(ProfileListEmpty, match="no profiles configured"):
        await sel.pick()


async def test_env_var_selector_round_robins_two_profiles() -> None:
    """EnvVarSelector with two profiles round-robins correctly over 4 picks."""
    sel = EnvVarSelector(profiles=["alpha", "beta"])

    picks = [await sel.pick() for _ in range(4)]
    names = [p.name for p in picks]

    assert names == ["alpha", "beta", "alpha", "beta"]


async def test_env_var_selector_returns_profile_info() -> None:
    """pick() returns a ProfileInfo with the correct name and empty env."""
    sel = EnvVarSelector(profiles=["my-profile"])
    info = await sel.pick()

    assert isinstance(info, ProfileInfo)
    assert info.name == "my-profile"
    assert info.env == {}


async def test_env_var_selector_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """EnvVarSelector reads CLAUDE_FLEET_PROFILES when no list is given."""
    monkeypatch.setenv("CLAUDE_FLEET_PROFILES", "roamhq,personal")

    sel = EnvVarSelector()  # no explicit list
    picks = [await sel.pick() for _ in range(4)]
    names = [p.name for p in picks]

    assert names == ["roamhq", "personal", "roamhq", "personal"]


async def test_env_var_selector_empty_env_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """EnvVarSelector raises ProfileListEmpty when env var is also empty."""
    monkeypatch.delenv("CLAUDE_FLEET_PROFILES", raising=False)

    sel = EnvVarSelector()
    with pytest.raises(ProfileListEmpty):
        await sel.pick()


# ---------------------------------------------------------------------------
# ClaudeLbSelector — mocked subprocess
# ---------------------------------------------------------------------------


async def test_claude_lb_selector_parses_json_output() -> None:
    """ClaudeLbSelector correctly parses claude-lb pick --json output."""
    payload = _lb_json("roamhq")
    mock_proc = _make_mock_proc(returncode=0, stdout=payload, stderr=b"")

    with patch(
        "claude_fleet.profile_selector.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=mock_proc,
    ):
        sel = ClaudeLbSelector(binary="/fake/claude-lb", pick_args=[])
        info = await sel.pick()

    assert isinstance(info, ProfileInfo)
    assert info.name == "roamhq"
    assert info.env == {}


async def test_claude_lb_selector_passes_pick_args() -> None:
    """ClaudeLbSelector passes pick_args to the subprocess command."""
    payload = _lb_json("work")
    mock_proc = _make_mock_proc(returncode=0, stdout=payload, stderr=b"")
    captured_cmd: list[str] = []

    async def fake_exec(*args: str, **kwargs: object) -> MagicMock:  # type: ignore[return]
        captured_cmd.extend(args)
        return mock_proc

    with patch("claude_fleet.profile_selector.asyncio.create_subprocess_exec", new=fake_exec):
        sel = ClaudeLbSelector(binary="/fake/claude-lb", pick_args=["--auto-refresh"])
        await sel.pick()

    assert "--auto-refresh" in captured_cmd
    assert "--json" in captured_cmd
    assert "pick" in captured_cmd


async def test_claude_lb_selector_binary_file_not_found_raises() -> None:
    """ClaudeLbSelector raises ClaudeLbBinaryMissing when binary is not found."""

    async def raise_fnf(*args: object, **kwargs: object) -> None:  # type: ignore[return]
        raise FileNotFoundError("No such file or directory: '/no/such/claude-lb'")

    with patch(
        "claude_fleet.profile_selector.asyncio.create_subprocess_exec",
        new=raise_fnf,
    ):
        sel = ClaudeLbSelector(binary="/no/such/claude-lb", pick_args=[])
        with pytest.raises(ClaudeLbBinaryMissing, match="claude-lb"):
            await sel.pick()


async def test_claude_lb_selector_missing_from_path_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClaudeLbSelector raises ClaudeLbBinaryMissing when binary is not on PATH."""
    with patch("claude_fleet.profile_selector.shutil.which", return_value=None):
        sel = ClaudeLbSelector()  # uses default "claude-lb" name
        with pytest.raises(ClaudeLbBinaryMissing, match="not on PATH"):
            await sel.pick()


async def test_claude_lb_selector_non_zero_exit_raises() -> None:
    """ClaudeLbSelector raises RuntimeError with stderr when subprocess fails."""
    mock_proc = _make_mock_proc(
        returncode=1,
        stdout=b"",
        stderr=b"no healthy profile available",
    )

    with patch(
        "claude_fleet.profile_selector.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=mock_proc,
    ):
        sel = ClaudeLbSelector(binary="/fake/claude-lb", pick_args=[])
        with pytest.raises(RuntimeError, match="no healthy profile available"):
            await sel.pick()


async def test_claude_lb_selector_malformed_json_raises() -> None:
    """ClaudeLbSelector raises RuntimeError when stdout is not valid JSON."""
    mock_proc = _make_mock_proc(
        returncode=0,
        stdout=b"not-json",
        stderr=b"",
    )

    with patch(
        "claude_fleet.profile_selector.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=mock_proc,
    ):
        sel = ClaudeLbSelector(binary="/fake/claude-lb", pick_args=[])
        with pytest.raises(RuntimeError, match="unexpected JSON"):
            await sel.pick()


async def test_claude_lb_selector_missing_data_key_raises() -> None:
    """ClaudeLbSelector raises RuntimeError when JSON is missing expected keys."""
    mock_proc = _make_mock_proc(
        returncode=0,
        stdout=json.dumps({"result": "ok"}).encode(),  # no "data.name"
        stderr=b"",
    )

    with patch(
        "claude_fleet.profile_selector.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=mock_proc,
    ):
        sel = ClaudeLbSelector(binary="/fake/claude-lb", pick_args=[])
        with pytest.raises(RuntimeError, match="unexpected JSON"):
            await sel.pick()


async def test_claude_lb_selector_empty_name_raises() -> None:
    """ClaudeLbSelector raises RuntimeError when returned profile name is empty."""
    mock_proc = _make_mock_proc(
        returncode=0,
        stdout=json.dumps({"data": {"name": ""}}).encode(),
        stderr=b"",
    )

    with patch(
        "claude_fleet.profile_selector.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=mock_proc,
    ):
        sel = ClaudeLbSelector(binary="/fake/claude-lb", pick_args=[])
        with pytest.raises(RuntimeError, match="empty profile name"):
            await sel.pick()


# ---------------------------------------------------------------------------
# WorkerBackend integration (profile env injection)
# ---------------------------------------------------------------------------


async def test_worker_backend_env_profiles_auto_wires_selector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When CLAUDE_FLEET_PROFILES is set, WorkerBackend auto-wires EnvVarSelector.

    We don't call spawn() (would need real git + claude binary); we just
    verify that _profile_selector is an EnvVarSelector and picks the expected
    profiles in order.
    """
    monkeypatch.setenv("CLAUDE_FLEET_PROFILES", "alice,bob")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from claude_fleet.orchestrator.worker_backend import WorkerBackend  # noqa: PLC0415

    backend = WorkerBackend(repo_root=tmp_path)

    assert backend._profile_selector is not None
    assert isinstance(backend._profile_selector, EnvVarSelector)

    picks = [await backend._profile_selector.pick() for _ in range(4)]
    names = [p.name for p in picks]
    assert names == ["alice", "bob", "alice", "bob"]


async def test_worker_backend_explicit_selector_used(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicitly injected ProfileSelector is used as-is, ignoring env."""
    monkeypatch.delenv("CLAUDE_FLEET_PROFILES", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from claude_fleet.orchestrator.worker_backend import WorkerBackend  # noqa: PLC0415

    sel = EnvVarSelector(profiles=["injected"])
    backend = WorkerBackend(repo_root=tmp_path, profile_selector=sel)

    assert backend._profile_selector is sel
    info = await backend._profile_selector.pick()
    assert info.name == "injected"


async def test_worker_backend_no_env_no_selector_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """WorkerBackend with no env var and no selector leaves _profile_selector as None."""
    monkeypatch.delenv("CLAUDE_FLEET_PROFILES", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from claude_fleet.orchestrator.worker_backend import WorkerBackend  # noqa: PLC0415

    backend = WorkerBackend(repo_root=tmp_path)
    assert backend._profile_selector is None
