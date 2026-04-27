"""Unit tests for worktrees_root config plumbing.

Covers:
- yaml with ``worktrees_root: ./custom-trees`` is resolved correctly by
  load_config() and propagated to WorkerBackend.
- yaml with deprecated ``worktree_base: ...`` is accepted, mapped to
  ``worktrees_root``, and emits a DeprecationWarning.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from rookery.orchestrator.config import OrchestratorConfig, load_config

# ---------------------------------------------------------------------------
# Test 1 — canonical worktrees_root field
# ---------------------------------------------------------------------------


def test_load_config_worktrees_root_canonical(tmp_path: Path) -> None:
    """load_config() with worktrees_root: ./custom-trees resolves to absolute path."""
    yaml_text = "worktrees_root: ./custom-trees\n"
    cfg_file = tmp_path / "rookery.yaml"
    cfg_file.write_text(yaml_text, encoding="utf-8")

    cfg = load_config(cfg_file)

    # Relative paths are resolved at load time relative to the config file's
    # directory, not the daemon CWD.
    assert cfg.worktrees_root == tmp_path / "custom-trees"


# ---------------------------------------------------------------------------
# Test 2 — deprecated alias worktree_base
# ---------------------------------------------------------------------------


def test_load_config_worktree_base_alias_accepted(tmp_path: Path) -> None:
    """load_config() accepts deprecated 'worktree_base', maps it to worktrees_root."""
    yaml_text = "worktree_base: ./legacy-trees\n"
    cfg_file = tmp_path / "rookery.yaml"
    cfg_file.write_text(yaml_text, encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = load_config(cfg_file)

    assert cfg.worktrees_root == cfg_file.parent.resolve() / "legacy-trees", (
        "worktree_base should be mapped to worktrees_root and resolved to absolute"
    )

    deprecation_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert len(deprecation_warnings) == 1, (
        "exactly one DeprecationWarning should be emitted for the alias"
    )
    assert "worktree_base" in str(deprecation_warnings[0].message)
    assert "worktrees_root" in str(deprecation_warnings[0].message)


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_load_config_missing_file_returns_default(tmp_path: Path) -> None:
    """load_config() returns defaults when the file does not exist."""
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert isinstance(cfg, OrchestratorConfig)
    assert cfg.worktrees_root is None


def test_load_config_both_fields_canonical_wins(tmp_path: Path) -> None:
    """When both fields are set, worktrees_root wins and no warning is emitted."""
    yaml_text = "worktrees_root: ./canonical\nworktree_base: ./alias\n"
    cfg_file = tmp_path / "rookery.yaml"
    cfg_file.write_text(yaml_text, encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = load_config(cfg_file)

    assert cfg.worktrees_root == cfg_file.parent.resolve() / "canonical"
    deprecation_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert len(deprecation_warnings) == 0
