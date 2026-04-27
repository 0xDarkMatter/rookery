"""Tests for rookery.parcel — scaffold + validation (G3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rookery.cli import app
from rookery.parcel import parcel_new, parcel_validate

# ---------------------------------------------------------------------------
# parcel_new tests
# ---------------------------------------------------------------------------


class TestParcelNew:
    def test_creates_file_with_correct_frontmatter(self, tmp_path: Path) -> None:
        target = tmp_path / "parcels" / "my-task.md"
        result = parcel_new("my-task", prompt_path=target)

        assert result == target
        assert target.exists()

        text = target.read_text(encoding="utf-8")
        assert "id: my-task" in text
        assert "priority: 0" in text
        assert "deps: []" in text
        assert "max_attempts: 3" in text
        assert "verification_enabled: true" in text
        assert "verdict_adapter: marker-file" in text

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c" / "parcel.md"
        parcel_new("parcel", prompt_path=deep)
        assert deep.exists()

    def test_default_path_is_parcels_slash_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = parcel_new("my-id")
        assert result == Path("parcels/my-id.md")
        assert (tmp_path / "parcels" / "my-id.md").exists()

    def test_body_contains_parcel_done_reference(self, tmp_path: Path) -> None:
        target = tmp_path / "abc.md"
        parcel_new("abc", prompt_path=target)
        text = target.read_text(encoding="utf-8")
        assert "PARCEL_DONE-abc.md" in text

    def test_refuses_overwrite_when_force_false(self, tmp_path: Path) -> None:
        target = tmp_path / "task.md"
        target.write_text("original", encoding="utf-8")

        with pytest.raises(FileExistsError):
            parcel_new("task", prompt_path=target, force=False)

        # Original content is preserved
        assert target.read_text(encoding="utf-8") == "original"

    def test_force_true_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "task.md"
        target.write_text("original", encoding="utf-8")

        result = parcel_new("task", prompt_path=target, force=True)

        assert result == target
        assert "id: task" in target.read_text(encoding="utf-8")

    def test_parcel_new_force_overwrites_existing(self, tmp_path: Path) -> None:
        """Calling parcel_new twice with force=True succeeds both times."""
        target = tmp_path / "regen.md"
        parcel_new("regen", prompt_path=target, force=True)
        first_content = target.read_text(encoding="utf-8")

        # Second call must succeed and produce valid parcel content
        result = parcel_new("regen", prompt_path=target, force=True)

        assert result == target
        second_content = target.read_text(encoding="utf-8")
        assert "id: regen" in second_content
        # Content is regenerated (identical template, but the write succeeded)
        assert second_content == first_content

    def test_parcel_new_no_force_raises_on_existing(self, tmp_path: Path) -> None:
        """Second call without force=True raises FileExistsError (regression guard)."""
        target = tmp_path / "once.md"
        parcel_new("once", prompt_path=target, force=False)

        with pytest.raises(FileExistsError):
            parcel_new("once", prompt_path=target, force=False)

        # Original content is preserved unchanged
        assert "id: once" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parcel_validate tests
# ---------------------------------------------------------------------------


def _write_parcel(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_GOOD_PARCEL = """\
---
id: good-task
priority: 0
deps: []
max_attempts: 3
verification_enabled: true
verdict_adapter: marker-file
notes: ""
---

# good-task

You are working in a fresh git worktree. Do the thing.

## Verdict

When you finish, write `PARCEL_DONE-good-task.md`.
"""


class TestParcelValidate:
    def test_happy_path(self, tmp_path: Path) -> None:
        path = tmp_path / "good-task.md"
        _write_parcel(path, _GOOD_PARCEL)

        result = parcel_validate(path)

        assert result.ok is True
        assert result.errors == []
        assert result.warnings == []

    def test_file_not_found(self, tmp_path: Path) -> None:
        result = parcel_validate(tmp_path / "missing.md")

        assert result.ok is False
        assert any("not found" in e for e in result.errors)

    def test_missing_id_field(self, tmp_path: Path) -> None:
        content = """\
---
priority: 0
deps: []
---

# no-id

Some body text here.
"""
        path = tmp_path / "no-id.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is False
        assert any("id" in e for e in result.errors)

    def test_mismatched_id_vs_filename(self, tmp_path: Path) -> None:
        content = """\
---
id: different-name
priority: 0
deps: []
---

# different-name

Some body text.
"""
        path = tmp_path / "actual-name.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is False
        assert any("does not match filename stem" in e for e in result.errors)

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        content = """\
---
id: broken
  invalid: : yaml: [
---

# broken

Some body.
"""
        path = tmp_path / "broken.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is False
        assert any("YAML" in e or "parse" in e.lower() for e in result.errors)

    def test_empty_body(self, tmp_path: Path) -> None:
        content = """\
---
id: empty-body
---
"""
        path = tmp_path / "empty-body.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is False
        assert any("body" in e for e in result.errors)

    def test_parcel_validate_empty_body_fails(self, tmp_path: Path) -> None:
        """Parcel with valid frontmatter but no body returns validation failure."""
        content = """\
---
id: no-body
priority: 0
deps: []
---
"""
        path = tmp_path / "no-body.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is False
        assert any(
            "parcel body is empty" in e and "claude -p" in e
            for e in result.errors
        )

    def test_parcel_validate_whitespace_only_body_fails(self, tmp_path: Path) -> None:
        """Body consisting solely of whitespace is treated as empty."""
        content = "---\nid: ws-body\npriority: 0\ndeps: []\n---\n\n   \n\n"
        path = tmp_path / "ws-body.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is False
        assert any(
            "parcel body is empty" in e and "claude -p" in e
            for e in result.errors
        )

    def test_warns_on_missing_parcel_done_reference(self, tmp_path: Path) -> None:
        content = """\
---
id: no-verdict
priority: 0
deps: []
---

# no-verdict

Do the thing, but we forgot to mention the verdict file.
"""
        path = tmp_path / "no-verdict.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        # Should be ok=True (warning, not error)
        assert result.ok is True
        assert result.errors == []
        assert any("PARCEL_DONE" in w for w in result.warnings)

    def test_wrong_type_priority(self, tmp_path: Path) -> None:
        content = """\
---
id: bad-types
priority: "high"
deps: []
---

# bad-types

Body here.
"""
        path = tmp_path / "bad-types.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is False
        assert any("priority" in e for e in result.errors)

    def test_wrong_type_deps_not_list(self, tmp_path: Path) -> None:
        content = """\
---
id: bad-deps
deps: "not-a-list"
---

# bad-deps

Body here.
"""
        path = tmp_path / "bad-deps.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is False
        assert any("deps" in e for e in result.errors)

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        content = "# Just a plain markdown file with no frontmatter.\n"
        path = tmp_path / "plain.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is False
        assert result.errors

    def test_notes_can_be_none(self, tmp_path: Path) -> None:
        content = """\
---
id: null-notes
notes: null
---

# null-notes

Body with `PARCEL_DONE-null-notes.md` reference.
"""
        path = tmp_path / "null-notes.md"
        _write_parcel(path, content)

        result = parcel_validate(path)

        assert result.ok is True
        assert result.errors == []


# ---------------------------------------------------------------------------
# CLI integration tests via CliRunner
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.chdir(tmp_path)
    return CliRunner()


class TestParcelCli:
    def test_parcel_new_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["parcel", "new", "--help"])
        assert result.exit_code == 0
        assert "Scaffold" in result.output or "parcel" in result.output.lower()

    def test_parcel_validate_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["parcel", "validate", "--help"])
        assert result.exit_code == 0

    def test_parcel_new_creates_file(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["parcel", "new", "my-job"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "parcels" / "my-job.md").exists()

    def test_parcel_new_with_custom_path(self, runner: CliRunner, tmp_path: Path) -> None:
        out = str(tmp_path / "custom.md")
        result = runner.invoke(app, ["parcel", "new", "custom", "--prompt", out])
        assert result.exit_code == 0, result.output
        assert Path(out).exists()

    def test_parcel_new_refuses_overwrite_without_force(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        runner.invoke(app, ["parcel", "new", "my-job"])
        result = runner.invoke(app, ["parcel", "new", "my-job"])
        assert result.exit_code != 0

    def test_parcel_new_force_overwrites(self, runner: CliRunner, tmp_path: Path) -> None:
        runner.invoke(app, ["parcel", "new", "my-job"])
        result = runner.invoke(app, ["parcel", "new", "my-job", "--force"])
        assert result.exit_code == 0, result.output

    def test_round_trip_new_then_validate(self, runner: CliRunner, tmp_path: Path) -> None:
        """parcel new then parcel validate on its output exits 0."""
        new_result = runner.invoke(app, ["parcel", "new", "round-trip"])
        assert new_result.exit_code == 0, new_result.output

        parcel_path = str(tmp_path / "parcels" / "round-trip.md")
        val_result = runner.invoke(app, ["parcel", "validate", parcel_path])
        assert val_result.exit_code == 0, val_result.output

    def test_parcel_validate_good_file_exits_0(self, runner: CliRunner, tmp_path: Path) -> None:
        path = tmp_path / "good.md"
        _write_parcel(path, _GOOD_PARCEL.replace("good-task", "good"))
        result = runner.invoke(app, ["parcel", "validate", str(path)])
        assert result.exit_code == 0

    def test_parcel_validate_bad_file_exits_1(self, runner: CliRunner, tmp_path: Path) -> None:
        path = tmp_path / "bad.md"
        _write_parcel(path, "# no frontmatter\n")
        result = runner.invoke(app, ["parcel", "validate", str(path)])
        assert result.exit_code != 0

    def test_parcel_validate_missing_file_exits_1(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(app, ["parcel", "validate", str(tmp_path / "ghost.md")])
        assert result.exit_code != 0

    def test_parcel_validate_json_mode(self, runner: CliRunner, tmp_path: Path) -> None:
        path = tmp_path / "good.md"
        _write_parcel(path, _GOOD_PARCEL.replace("good-task", "good"))
        result = runner.invoke(app, ["parcel", "validate", str(path), "--json"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["errors"] == []
