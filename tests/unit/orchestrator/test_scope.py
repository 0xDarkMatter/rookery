"""Unit tests for :mod:`claude_fleet.orchestrator.scope`.

Covers the three layers independently:

- frontmatter parsing (YAML block extraction + pydantic validation)
- pattern-vs-pattern overlap rules (glob, directory prefix, literal)
- conflict aggregation (owns / modifies matrix; reads ignored)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_fleet.orchestrator.scope import (
    ParcelClaims,
    check_pair,
    detect_conflicts,
    load_claims,
    parse_frontmatter,
    patterns_overlap,
)

# --- frontmatter parsing ----------------------------------------------------


def test_parse_frontmatter_happy_path() -> None:
    text = (
        "---\n"
        "id: W20-test\n"
        "owns:\n"
        "  - src/claude_fleet/orchestrator/scope.py\n"
        "reads:\n"
        "  - docs/PARCELS.md\n"
        "---\n"
        "\n# Parcel W20\n"
    )
    fm = parse_frontmatter(text)
    assert fm is not None
    assert fm["id"] == "W20-test"
    assert fm["owns"] == ["src/claude_fleet/orchestrator/scope.py"]


def test_parse_frontmatter_missing_returns_none() -> None:
    assert parse_frontmatter("# Parcel no-frontmatter\n") is None


def test_parse_frontmatter_non_dict_returns_none() -> None:
    # YAML scalar/list between fences isn't a claim record; swallow silently.
    text = "---\n- a\n- b\n---\n"
    assert parse_frontmatter(text) is None


def test_parse_frontmatter_invalid_yaml_raises() -> None:
    text = "---\nid: [unclosed\n---\n"
    with pytest.raises(ValueError, match="invalid YAML frontmatter"):
        parse_frontmatter(text)


def test_load_claims_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_claims(tmp_path / "does-not-exist.md") is None


def test_load_claims_parses_full_schema(tmp_path: Path) -> None:
    p = tmp_path / "W20.md"
    p.write_text(
        "---\n"
        "id: W20\n"
        "priority: P1\n"
        "lane: building\n"
        "owns: [a.py, b.py]\n"
        "modifies: [c.py]\n"
        "reads: [d.py]\n"
        "forbidden: [e/**]\n"
        "depends_on: [W1, W2]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    claims = load_claims(p)
    assert claims is not None
    assert claims.id == "W20"
    assert claims.owns == ["a.py", "b.py"]
    assert claims.modifies == ["c.py"]
    assert claims.reads == ["d.py"]
    assert claims.forbidden == ["e/**"]
    assert claims.depends_on == ["W1", "W2"]


def test_load_claims_defaults_id_to_stem_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "W42-stem-default.md"
    p.write_text(
        "---\nowns: [x.py]\n---\n\nbody\n",
        encoding="utf-8",
    )
    claims = load_claims(p)
    assert claims is not None
    assert claims.id == "W42-stem-default"


# --- pattern overlap --------------------------------------------------------


def test_overlap_equal_paths() -> None:
    assert patterns_overlap(
        "src/claude_fleet/orchestrator/cli.py",
        "src/claude_fleet/orchestrator/cli.py",
    )


def test_overlap_directory_prefix_vs_file() -> None:
    assert patterns_overlap(
        "src/claude_fleet/platform/",
        "src/claude_fleet/platform/config.py",
    )


def test_overlap_file_vs_directory_prefix_symmetric() -> None:
    # Symmetry: the order of operands must not matter for directory prefixes.
    assert patterns_overlap(
        "src/claude_fleet/platform/config.py",
        "src/claude_fleet/platform/",
    )


def test_overlap_glob_double_star_matches_literal() -> None:
    assert patterns_overlap(
        "src/claude_fleet/orchestrator/**",
        "src/claude_fleet/orchestrator/cli.py",
    )


def test_overlap_glob_single_star_matches_fname() -> None:
    assert patterns_overlap(
        "src/claude_fleet/orchestrator/*.py",
        "src/claude_fleet/orchestrator/cli.py",
    )


def test_overlap_glob_does_not_match_unrelated_subtree() -> None:
    assert not patterns_overlap(
        "src/claude_fleet/orchestrator/**",
        "src/claude_fleet/codex/retrieval.py",
    )


def test_overlap_distinct_files() -> None:
    assert not patterns_overlap(
        "src/claude_fleet/orchestrator/cli.py",
        "src/claude_fleet/orchestrator/daemon.py",
    )


def test_overlap_both_globs_shared_stem() -> None:
    # Both patterns root at src/claude_fleet/orchestrator/; conservatively flag.
    assert patterns_overlap(
        "src/claude_fleet/orchestrator/**",
        "src/claude_fleet/orchestrator/*.py",
    )


def test_overlap_both_globs_disjoint_stems() -> None:
    assert not patterns_overlap(
        "src/claude_fleet/orchestrator/**",
        "src/claude_fleet/codex/**",
    )


def test_overlap_trailing_slash_directory() -> None:
    assert patterns_overlap(
        "tests/unit/orchestrator/",
        "tests/unit/orchestrator/test_scope.py",
    )


# --- conflict aggregation ---------------------------------------------------


def _c(**kwargs: object) -> ParcelClaims:
    """Build a ParcelClaims with an id + whichever lists the test needs."""
    kwargs.setdefault("id", "X")
    return ParcelClaims.model_validate(kwargs)


def test_conflict_owns_vs_owns_is_hard() -> None:
    a = _c(id="A", owns=["src/claude_fleet/orchestrator/cli.py"])
    b = _c(id="B", owns=["src/claude_fleet/orchestrator/cli.py"])
    report = detect_conflicts(a, [b])
    assert report.has_hard
    assert report.has_any
    assert report.conflicts[0].other_parcel_id == "B"
    assert report.conflicts[0].overlaps[0].severity == "hard"
    assert report.conflicts[0].overlaps[0].kind == "owns-vs-owns"


def test_conflict_owns_vs_modifies_is_warn() -> None:
    a = _c(id="A", owns=["src/claude_fleet/orchestrator/cli.py"])
    b = _c(id="B", modifies=["src/claude_fleet/orchestrator/cli.py"])
    report = detect_conflicts(a, [b])
    assert report.has_any
    assert not report.has_hard
    assert report.conflicts[0].overlaps[0].severity == "warn"
    assert report.conflicts[0].overlaps[0].kind == "owns-vs-modifies"


def test_conflict_modifies_vs_modifies_is_warn() -> None:
    a = _c(id="A", modifies=["src/claude_fleet/orchestrator/cli.py"])
    b = _c(id="B", modifies=["src/claude_fleet/orchestrator/cli.py"])
    report = detect_conflicts(a, [b])
    assert report.has_any
    assert not report.has_hard
    assert report.conflicts[0].overlaps[0].kind == "modifies-vs-modifies"


def test_conflict_reads_vs_reads_is_clean() -> None:
    # reads-vs-reads is informational — no conflict whatever the paths.
    a = _c(id="A", reads=["src/claude_fleet/orchestrator/cli.py"])
    b = _c(id="B", reads=["src/claude_fleet/orchestrator/cli.py"])
    report = detect_conflicts(a, [b])
    assert not report.has_any


def test_conflict_owns_vs_reads_is_clean() -> None:
    # Reading someone else's owned file is explicitly allowed.
    a = _c(id="A", owns=["src/claude_fleet/orchestrator/cli.py"])
    b = _c(id="B", reads=["src/claude_fleet/orchestrator/cli.py"])
    report = detect_conflicts(a, [b])
    assert not report.has_any


def test_conflict_skips_self() -> None:
    # Enqueueing the same id twice must not self-conflict in the report.
    a = _c(id="same", owns=["src/x.py"])
    b = _c(id="same", owns=["src/x.py"])
    report = detect_conflicts(a, [b])
    assert not report.has_any


def test_conflict_aggregates_multiple_parcels() -> None:
    a = _c(id="A", owns=["src/x.py", "src/y.py"])
    b = _c(id="B", owns=["src/x.py"])
    c = _c(id="C", owns=["src/y.py"])
    clean = _c(id="D", owns=["src/z.py"])
    report = detect_conflicts(a, [b, c, clean])
    assert {cf.other_parcel_id for cf in report.conflicts} == {"B", "C"}


def test_check_pair_glob_expansion() -> None:
    a = _c(id="A", owns=["src/claude_fleet/orchestrator/**"])
    b = _c(id="B", owns=["src/claude_fleet/orchestrator/cli.py"])
    conflict = check_pair(a, b)
    assert conflict is not None
    assert conflict.has_hard
    assert conflict.overlaps[0].our_pattern == "src/claude_fleet/orchestrator/**"
    assert conflict.overlaps[0].their_pattern == "src/claude_fleet/orchestrator/cli.py"


def test_check_pair_returns_none_when_clean() -> None:
    a = _c(id="A", owns=["src/x.py"])
    b = _c(id="B", owns=["src/y.py"])
    assert check_pair(a, b) is None


def test_empty_claims_produce_no_conflicts() -> None:
    # A parcel with no owns/modifies can never conflict.
    a = _c(id="A")
    b = _c(id="B", owns=["src/x.py"], modifies=["src/y.py"])
    report = detect_conflicts(a, [b])
    assert not report.has_any
