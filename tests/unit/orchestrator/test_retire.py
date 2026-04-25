"""Gate-by-gate tests for :mod:`claude_fleet.orchestrator.retire`.

Each gate is driven deterministically by monkeypatching the module-level
probe helpers. The happy-path test exercises the real ``git worktree
remove`` invocation against a real repo so the end-to-end subprocess
chain is also covered.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_fleet.orchestrator import retire as retire_mod
from claude_fleet.orchestrator.backend import Job


def _make_job(job_id: str = "W21-demo", *, status: str = "landed") -> Job:
    return Job(
        id=job_id,
        prompt_path=f"/tmp/{job_id}.md",
        status=status,  # type: ignore[arg-type]
        land_attempts=1,
        landed_commit="deadbeef",
    )


def _all_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every probe except mtime + existence to report 'safe'."""
    monkeypatch.setattr(retire_mod, "_git_porcelain", lambda wt: "")
    monkeypatch.setattr(retire_mod, "_is_ancestor", lambda repo, br, main: True)
    monkeypatch.setattr(retire_mod, "_has_open_files", lambda wt: False)


def _make_worktree(tmp_path: Path, job_id: str) -> Path:
    """Materialise ``<worktrees_root>/<job_id>`` with a sentinel file."""
    wt = tmp_path / "worktrees" / job_id
    wt.mkdir(parents=True)
    (wt / "sentinel.txt").write_text("hi")
    return wt


# ---------------------------------------------------------------------------
# can_auto_retire: each gate in isolation
# ---------------------------------------------------------------------------


def test_gate_job_not_landed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _all_green(monkeypatch)
    job = _make_job(status="done")  # not 'landed'
    _make_worktree(tmp_path, job.id)
    check = retire_mod.can_auto_retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=tmp_path / "repo",
        idle_seconds=0,
    )
    assert check.ok is False
    assert check.reason == "job_not_landed"


def test_gate_worktree_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _all_green(monkeypatch)
    job = _make_job()
    # Deliberately skip _make_worktree — the dir isn't created.
    check = retire_mod.can_auto_retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=tmp_path / "repo",
        idle_seconds=0,
    )
    assert check.ok is False
    assert check.reason == "worktree_missing"


def test_gate_recent_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _all_green(monkeypatch)
    job = _make_job()
    _make_worktree(tmp_path, job.id)
    # Pin the idle clock so the sentinel's newly-written mtime is < idle window.
    check = retire_mod.can_auto_retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=tmp_path / "repo",
        idle_seconds=3600,
    )
    assert check.ok is False
    assert check.reason == "recent_write"


def test_gate_uncommitted_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _all_green(monkeypatch)
    monkeypatch.setattr(
        retire_mod, "_git_porcelain", lambda wt: " M somefile.txt\n"
    )
    job = _make_job()
    _make_worktree(tmp_path, job.id)
    check = retire_mod.can_auto_retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=tmp_path / "repo",
        idle_seconds=0,  # idle gate passes
    )
    assert check.ok is False
    assert check.reason == "uncommitted_changes"


def test_gate_branch_not_merged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _all_green(monkeypatch)
    monkeypatch.setattr(retire_mod, "_is_ancestor", lambda repo, br, main: False)
    job = _make_job()
    _make_worktree(tmp_path, job.id)
    check = retire_mod.can_auto_retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=tmp_path / "repo",
        idle_seconds=0,
    )
    assert check.ok is False
    assert check.reason == "branch_not_merged"


def test_gate_active_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _all_green(monkeypatch)
    monkeypatch.setattr(retire_mod, "_has_open_files", lambda wt: True)
    job = _make_job()
    _make_worktree(tmp_path, job.id)
    check = retire_mod.can_auto_retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=tmp_path / "repo",
        idle_seconds=0,
    )
    assert check.ok is False
    assert check.reason == "active_process"


def test_all_gates_green(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _all_green(monkeypatch)
    job = _make_job()
    _make_worktree(tmp_path, job.id)
    check = retire_mod.can_auto_retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=tmp_path / "repo",
        idle_seconds=0,
    )
    assert check.ok is True
    assert check.reason is None


def test_empty_worktree_passes_idle_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty worktree has no mtime. The idle gate must not crash."""
    _all_green(monkeypatch)
    job = _make_job()
    wt = tmp_path / "worktrees" / job.id
    wt.mkdir(parents=True)  # empty dir
    check = retire_mod.can_auto_retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=tmp_path / "repo",
        idle_seconds=3600,
    )
    assert check.ok is True


# ---------------------------------------------------------------------------
# retire() end-to-end — real git subprocess chain
# ---------------------------------------------------------------------------


def _init_repo_with_worktree(
    tmp_path: Path, job_id: str, *, parcel_done: bool = True
) -> tuple[Path, Path]:
    """Build a tmp git repo + a merged worktree we can retire. Returns (repo_root, worktree_path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    def git(*args: str, cwd: Path | None = None) -> None:
        subprocess.run(
            ["git", "-C", str(cwd or repo), *args], check=True, capture_output=True
        )

    git("init", "--initial-branch=main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("commit", "--allow-empty", "-m", "root")

    branch = f"parcel/{job_id}"
    git("branch", branch)

    wt_path = worktrees / job_id
    git("worktree", "add", str(wt_path), branch)

    # Make a commit on the parcel branch…
    (wt_path / "feature.txt").write_text("new feature")
    git("add", "feature.txt", cwd=wt_path)
    git("commit", "-m", "add feature", cwd=wt_path)

    if parcel_done:
        (wt_path / f"PARCEL_DONE-{job_id}.md").write_text("# done\n")
        git("add", f"PARCEL_DONE-{job_id}.md", cwd=wt_path)
        git("commit", "-m", f"docs: PARCEL_DONE for {job_id}", cwd=wt_path)

    # …then merge it into main so the ancestor check passes.
    git("merge", "--ff-only", branch)

    return repo, wt_path


def test_retire_happy_path_removes_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = _make_job("happy")
    repo, wt_path = _init_repo_with_worktree(tmp_path, job.id)

    # Backdate every file so the idle gate passes without sleeping.
    _backdate(wt_path)

    # Active-process probe: stub to False.
    monkeypatch.setattr(retire_mod, "_has_open_files", lambda wt: False)

    project_root = tmp_path / "project"
    project_root.mkdir()

    result = retire_mod.retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=repo,
        project_root=project_root,
        idle_seconds=1,
    )

    assert not wt_path.exists()
    assert result.parcel_done_copied is not None
    assert result.parcel_done_copied.exists()
    assert result.parcel_done_copied.name == f"{job.id}-PARCEL_DONE.md"


def test_retire_without_parcel_done_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = _make_job("no-pd")
    repo, wt_path = _init_repo_with_worktree(tmp_path, job.id, parcel_done=False)
    _backdate(wt_path)
    monkeypatch.setattr(retire_mod, "_has_open_files", lambda wt: False)
    project_root = tmp_path / "project"
    project_root.mkdir()

    result = retire_mod.retire(
        job,
        worktrees_root=tmp_path / "worktrees",
        repo_root=repo,
        project_root=project_root,
        idle_seconds=1,
    )

    assert not wt_path.exists()
    assert result.parcel_done_copied is None


def test_retire_refuses_when_gate_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """retire() must re-check the gates; caller contract is preconditions hold."""
    job = _make_job("gate-fail", status="done")  # wrong status
    check = retire_mod.can_auto_retire(
        job,
        worktrees_root=tmp_path,
        repo_root=tmp_path,
        idle_seconds=0,
    )
    assert check.ok is False

    with pytest.raises(RuntimeError, match="job_not_landed"):
        retire_mod.retire(
            job,
            worktrees_root=tmp_path,
            repo_root=tmp_path,
            project_root=tmp_path,
            idle_seconds=0,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _backdate(path: Path) -> None:
    """Set every file mtime to an hour in the past so the idle gate passes."""
    past = 1_600_000_000.0  # arbitrary fixed epoch well in the past
    for entry in path.rglob("*"):
        try:
            os.utime(entry, (past, past))
        except OSError:
            continue
