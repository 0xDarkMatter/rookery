"""WorkerBackend tests — stub the platform spawn + worktree calls.

Real spawning runs ``claude -p`` in a git worktree; tests must not do
either. We monkeypatch ``ensure_worktree`` + ``spawn_headless_claude`` +
``find_parcel_prompt`` so the backend exercises its own logic (env
building, pid tracking, harvest) against a real subprocess that writes a
real ``.pid`` — no claude, no git.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rookery.orchestrator.backend import Job
from rookery.platform.headless_spawn import SpawnResult

IS_WINDOWS = sys.platform == "win32"


def _spawn_sleeper(worktree: Path, log_path: Path) -> int:
    """Spawn a detached python worker that sleeps; return its native pid.

    Side effect: writes ``.pid`` + ``.winpid`` in *worktree*, mirroring
    what :func:`spawn_headless_claude` does in production.
    """
    worker_py = textwrap.dedent(
        """
        import time
        time.sleep(600)
        """
    ).strip()
    worktree.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    popen_kwargs: dict[str, Any] = {"cwd": str(worktree)}
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    log_fh = log_path.open("ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 — test-only, argv fully controlled
            [sys.executable, "-c", worker_py],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
    finally:
        log_fh.close()

    pid = proc.pid
    (worktree / ".pid").write_text(f"{pid}\n", encoding="utf-8")
    (worktree / ".winpid").write_text(f"{pid}\n", encoding="utf-8")
    return pid


@pytest.fixture()
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """WorkerBackend with platform calls stubbed to spawn a benign sleeper."""

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "parcels").mkdir()
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()

    def fake_find_parcel_prompt(parcels_dir: Path, name: str) -> Path:
        path = parcels_dir / f"{name}.md"
        path.write_text("# test parcel\n", encoding="utf-8")
        return path

    def fake_ensure_worktree(parcel_id: str, repo_root: Path, **_kwargs) -> Path:
        wt = worktrees_root / parcel_id
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    def fake_spawn_headless_claude(
        *,
        worktree: Path,
        prompt_path: Path,
        log_path: Path,
        **_kwargs,
    ) -> SpawnResult:
        pid = _spawn_sleeper(worktree, log_path)
        (log_path).parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"launched {worktree.name}\n")
        return SpawnResult(
            pid=pid,
            worktree=worktree,
            log_path=log_path,
            prompt_path=prompt_path,
            prompt_bytes=prompt_path.stat().st_size,
        )

    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.find_parcel_prompt",
        fake_find_parcel_prompt,
    )
    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.ensure_worktree",
        fake_ensure_worktree,
    )
    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.spawn_headless_claude",
        fake_spawn_headless_claude,
    )
    # Stub claude-lb so tests don't shell out to the real CLI on every spawn.
    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.claude_lb.refresh_expired",
        lambda: None,
    )
    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.claude_lb.pick_profile",
        lambda **_kw: None,
    )
    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.claude_lb.resolve_config_dir",
        lambda _p: None,
    )
    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.claude_lb.bin_dir",
        lambda: None,
    )

    from rookery.orchestrator.worker_backend import WorkerBackend

    return WorkerBackend(
        repo_root=repo_root,
        worktrees_root=worktrees_root,
        env_overrides={
            "ANTHROPIC_API_KEY": "",  # '' = unset (clears any host-env leak)
            "CLAUDE_CODE_OAUTH_TOKEN": "fake-token",
        },
    )


def _cleanup_pid(pid: int | None) -> None:
    if pid is None or pid < 0:
        return
    try:
        os.kill(pid, 9 if not IS_WINDOWS else 15)
    except OSError:
        pass


async def test_spawn_creates_handle_with_pid(backend) -> None:
    job = Job(id="J1", prompt_path="/tmp/J1.md")
    handle = await backend.spawn(job)

    try:
        assert handle.job_id == "J1"
        assert handle.worker_id.startswith("local-")
        assert handle.pid is not None and handle.pid > 0
        assert handle.worktree.exists()
        assert (handle.worktree / ".pid").exists()

        alive = await backend.is_alive(handle)
        assert alive is True
    finally:
        _cleanup_pid(handle.pid)


async def test_harvest_returns_none_without_parcel_done(backend) -> None:
    job = Job(id="J2", prompt_path="/tmp/J2.md")
    handle = await backend.spawn(job)
    try:
        result = await backend.harvest(handle)
        assert result is None
    finally:
        _cleanup_pid(handle.pid)


async def test_harvest_parses_parcel_done_md(backend) -> None:
    """Harvest finds PARCEL_DONE-<job_id>.md (new convention)."""
    job = Job(id="J3", prompt_path="/tmp/J3.md")
    handle = await backend.spawn(job)
    try:
        done_file = handle.worktree / f"PARCEL_DONE-{job.id}.md"
        done_file.write_text("# PARCEL_DONE\n\nAll good.\n", encoding="utf-8")

        result = await backend.harvest(handle)
        assert result is not None
        assert result["status"] == "done"
        assert "All good" in str(result["parcel_done_md"])
        assert "launched J3" in str(result["stdout_tail"])
    finally:
        _cleanup_pid(handle.pid)


async def test_harvest_falls_back_to_legacy_parcel_done_md(backend) -> None:
    """Legacy plain PARCEL_DONE.md still recognised for backward compatibility."""
    job = Job(id="J3b", prompt_path="/tmp/J3b.md")
    handle = await backend.spawn(job)
    try:
        done_file = handle.worktree / "PARCEL_DONE.md"
        done_file.write_text("# PARCEL_DONE\n\nLegacy.\n", encoding="utf-8")

        result = await backend.harvest(handle)
        assert result is not None
        assert result["status"] == "done"
        assert "Legacy" in str(result["parcel_done_md"])
    finally:
        _cleanup_pid(handle.pid)


async def test_harvest_prefers_new_over_legacy(backend) -> None:
    """When both files exist, harvest reads the per-parcel one (source of truth)."""
    job = Job(id="J3c", prompt_path="/tmp/J3c.md")
    handle = await backend.spawn(job)
    try:
        (handle.worktree / f"PARCEL_DONE-{job.id}.md").write_text("new", encoding="utf-8")
        (handle.worktree / "PARCEL_DONE.md").write_text("legacy", encoding="utf-8")

        result = await backend.harvest(handle)
        assert result is not None
        assert "new" in str(result["parcel_done_md"])
        assert "legacy" not in str(result["parcel_done_md"])
    finally:
        _cleanup_pid(handle.pid)


async def test_terminate_kills_worker(backend) -> None:
    job = Job(id="J4", prompt_path="/tmp/J4.md")
    handle = await backend.spawn(job)
    assert await backend.is_alive(handle) is True

    await backend.terminate(handle)

    # After terminate, is_alive may lag a bit; poll.
    for _ in range(20):
        if not await backend.is_alive(handle):
            break
        import asyncio  # noqa: PLC0415
        await asyncio.sleep(0.1)
    assert await backend.is_alive(handle) is False


async def test_spawn_refuses_when_api_key_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rookery.orchestrator.worker_backend import WorkerBackend

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "parcels").mkdir()
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-rejected")

    backend = WorkerBackend(
        repo_root=repo_root,
        worktrees_root=worktrees_root,
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        await backend.spawn(Job(id="bad", prompt_path="/tmp/bad.md"))


async def test_spawn_failure_surfaces_as_runtime_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the platform spawn raises, WorkerBackend wraps it in RuntimeError."""
    from rookery.orchestrator.worker_backend import WorkerBackend

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "parcels").mkdir()
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()

    def fake_find(parcels_dir: Path, name: str) -> Path:
        p = parcels_dir / f"{name}.md"
        p.write_text("x", encoding="utf-8")
        return p

    def fake_ensure(parcel_id, repo_root, **_kwargs):
        wt = worktrees_root / parcel_id
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    def boom(**_kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr("rookery.orchestrator.worker_backend.find_parcel_prompt", fake_find)
    monkeypatch.setattr("rookery.orchestrator.worker_backend.ensure_worktree", fake_ensure)
    monkeypatch.setattr("rookery.orchestrator.worker_backend.spawn_headless_claude", boom)

    backend = WorkerBackend(
        repo_root=repo_root,
        worktrees_root=worktrees_root,
        env_overrides={
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "fake",
        },
    )

    with pytest.raises(RuntimeError, match="spawn failed for explode"):
        await backend.spawn(Job(id="explode", prompt_path="/tmp/x.md"))


async def test_spawn_missing_prompt_surfaces_as_runtime_error(tmp_path: Path) -> None:
    """Typo'd job id → find_parcel_prompt raises → WorkerBackend wraps."""
    from rookery.orchestrator.worker_backend import WorkerBackend

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "parcels").mkdir()
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()

    backend = WorkerBackend(
        repo_root=repo_root,
        worktrees_root=worktrees_root,
        env_overrides={
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "fake",
        },
    )

    with pytest.raises(RuntimeError, match="prompt not found"):
        await backend.spawn(Job(id="nonexistent-parcel", prompt_path="/tmp/x.md"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only PATH fix")
def test_augment_windows_path_prepends_claude_or_git_bin() -> None:
    """Bare PATH (as pm2/Task Scheduler daemons inherit) should get claude
    bin dir and/or Git POSIX utility dirs prepended."""
    from rookery.orchestrator.worker_backend import _augment_windows_path

    bare_path = r"C:\Windows\System32;C:\Windows"
    augmented = _augment_windows_path(bare_path)

    # At least one of: claude.exe OR dirname.exe must now be reachable.
    found_claude_or_dirname = False
    for entry in augmented.split(os.pathsep):
        p = Path(entry)
        if (p / "claude.exe").is_file() or (p / "dirname.exe").is_file():
            found_claude_or_dirname = True
            break
    assert found_claude_or_dirname, (
        f"neither claude.exe nor dirname.exe reachable from augmented PATH; "
        f"got: {augmented!r}"
    )
    # Original entries must still be present (prepend, not replace).
    assert r"C:\Windows\System32" in augmented


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only profile resolution")
def test_resolve_claude_config_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ROOKERY_CONFIG_DIR wins over profile arg."""
    from rookery.orchestrator.worker_backend import _resolve_claude_config_dir

    override = tmp_path / "custom-profile"
    override.mkdir()
    monkeypatch.setenv("ROOKERY_CONFIG_DIR", str(override))

    assert _resolve_claude_config_dir("mknv74") == str(override)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only profile resolution")
def test_resolve_claude_config_dir_profile_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """ROOKERY_PROFILE overrides the profile arg; resolves via C:\\Users\\* scan."""
    from rookery.orchestrator.worker_backend import _resolve_claude_config_dir

    monkeypatch.delenv("ROOKERY_CONFIG_DIR", raising=False)
    monkeypatch.setenv("ROOKERY_PROFILE", "mknv74")

    result = _resolve_claude_config_dir("roamhq")  # ROOKERY_PROFILE wins
    # Result should be an mknv74 profile dir, if one exists on this machine.
    if result is not None:
        assert result.endswith("mknv74")


async def test_next_profile_delegates_to_claude_lb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When claude-lb returns a profile, WorkerBackend uses it (not the ring)."""
    from rookery.orchestrator.worker_backend import WorkerBackend

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()

    # claude-lb picks "evolution7" regardless of config default "mknv74".
    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.claude_lb.pick_profile",
        lambda **_kw: "evolution7",
    )

    backend = WorkerBackend(
        repo_root=repo_root,
        worktrees_root=worktrees_root,
        claude_profile="mknv74",  # would be used absent claude-lb
    )

    assert backend._next_profile() == "evolution7"


async def test_next_profile_falls_back_when_claude_lb_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When claude-lb returns None, WorkerBackend uses its ring / default."""
    from rookery.orchestrator.worker_backend import WorkerBackend

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()

    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.claude_lb.pick_profile",
        lambda **_kw: None,
    )

    backend = WorkerBackend(
        repo_root=repo_root,
        worktrees_root=worktrees_root,
        claude_profile="mknv74",
    )

    assert backend._next_profile() == "mknv74"


def test_resolve_config_dir_prefers_claude_lb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When claude-lb resolves a dir for the profile, it wins over C:\\Users scan."""
    from rookery.orchestrator.worker_backend import _resolve_claude_config_dir

    expected = tmp_path / "fake-roamhq"
    expected.mkdir()

    monkeypatch.delenv("ROOKERY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ROOKERY_PROFILE", raising=False)
    monkeypatch.setattr(
        "rookery.orchestrator.worker_backend.claude_lb.resolve_config_dir",
        lambda _p: str(expected),
    )

    assert _resolve_claude_config_dir("roamhq") == str(expected)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only PATH fix")
def test_augment_windows_path_is_idempotent() -> None:
    """Re-augmenting an already-augmented PATH must not duplicate entries."""
    from rookery.orchestrator.worker_backend import _augment_windows_path

    once = _augment_windows_path("")
    twice = _augment_windows_path(once)

    assert once == twice, "augmentation is not idempotent"
