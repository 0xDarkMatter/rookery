"""Unit tests: worktrees_root config value threads through to GitWorktreeLifecycle.

Asserts:
- Yaml with ``worktrees_root: ./custom-trees`` resolves the lifecycle to use
  ``<config-dir>/custom-trees`` (not a hardcoded system temp path).
- Yaml with an absolute path is honored as-is.
- No yaml file (no --config) defaults to ``./worktrees`` relative to CWD,
  NEVER to paths like ``../claude-fleet-worktrees``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_fleet.orchestrator.config import OrchestratorConfig, load_config
from claude_fleet.worktree import GitWorktreeLifecycle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lifecycle(cfg: OrchestratorConfig, repo_root: Path) -> GitWorktreeLifecycle:
    """Replicate the wiring that __main__.start_cmd does."""
    worktrees_root = cfg.worktrees_root or (repo_root / "worktrees")
    return GitWorktreeLifecycle(base_dir=worktrees_root, repo_root=repo_root)


# ---------------------------------------------------------------------------
# Test 1 — relative path in yaml resolves to config-dir, not CWD or parent
# ---------------------------------------------------------------------------


def test_relative_worktrees_root_resolves_to_config_dir(tmp_path: Path) -> None:
    """``worktrees_root: ./custom-trees`` anchors to the yaml directory."""
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    cfg_file = config_dir / "claude-fleet.yaml"
    cfg_file.write_text("worktrees_root: ./custom-trees\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    cfg = load_config(cfg_file)

    lifecycle = _make_lifecycle(cfg, repo_root)

    expected = config_dir.resolve() / "custom-trees"
    assert lifecycle.base_dir == expected, (
        f"lifecycle.base_dir should be {expected!r}, got {lifecycle.base_dir!r}"
    )
    # Verify the hardcoded fallback is NOT used
    assert "claude-fleet-worktrees" not in str(lifecycle.base_dir)


# ---------------------------------------------------------------------------
# Test 2 — absolute path in yaml is honored unchanged
# ---------------------------------------------------------------------------


def test_absolute_worktrees_root_honored_as_is(tmp_path: Path) -> None:
    """An absolute ``worktrees_root`` path passes through without alteration."""
    abs_root = tmp_path / "absolute" / "my-trees"
    cfg_file = tmp_path / "claude-fleet.yaml"
    cfg_file.write_text(f"worktrees_root: {abs_root}\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    cfg = load_config(cfg_file)

    lifecycle = _make_lifecycle(cfg, repo_root)

    assert lifecycle.base_dir == abs_root, (
        f"lifecycle.base_dir should be {abs_root!r}, got {lifecycle.base_dir!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — no config file → default ./worktrees, NOT ../claude-fleet-worktrees
# ---------------------------------------------------------------------------


def test_no_config_defaults_to_cwd_worktrees(tmp_path: Path) -> None:
    """When no config file is provided, worktrees land in ``<repo_root>/worktrees``.

    The legacy fallback ``<repo_root>/../claude-fleet-worktrees`` (from
    ``worktree_dir.repo_paths()``) must NOT be used.
    """
    repo_root = tmp_path / "myrepo"
    cfg = load_config(None)  # no config file

    lifecycle = _make_lifecycle(cfg, repo_root)

    expected = repo_root / "worktrees"
    assert lifecycle.base_dir == expected, (
        f"lifecycle.base_dir should be {expected!r}, got {lifecycle.base_dir!r}"
    )
    # The hardcoded fallback path must never appear
    assert "claude-fleet-worktrees" not in str(lifecycle.base_dir)
    # Must not be a parent-level path
    assert lifecycle.base_dir.parent == repo_root, (
        "default worktrees dir should be a child of repo_root, not a sibling"
    )


# ---------------------------------------------------------------------------
# Test 4 — lifecycle.repo_root is set to repo_root (not base_dir)
# ---------------------------------------------------------------------------


def test_lifecycle_repo_root_matches_repo_root(tmp_path: Path) -> None:
    """GitWorktreeLifecycle.repo_root is the actual repo root (for git -C)."""
    cfg_file = tmp_path / "claude-fleet.yaml"
    cfg_file.write_text("worktrees_root: ./wt\n", encoding="utf-8")

    repo_root = tmp_path / "the-repo"
    cfg = load_config(cfg_file)
    lifecycle = _make_lifecycle(cfg, repo_root)

    assert lifecycle.repo_root == repo_root
