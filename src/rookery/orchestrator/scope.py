"""Parcel scope claims + pre-enqueue conflict detection.

Each parcel markdown file may carry a YAML frontmatter block declaring
which paths the parcel ``owns`` (exclusive write), ``modifies`` (edits
but may coexist), ``reads`` (informational), and refuses to touch
(``forbidden``). This module parses that frontmatter and detects
overlapping claims between a candidate parcel and the set of jobs
already in flight, so the queue can refuse or warn before two parallel
parcels rediscover the same conflict at rebase time.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# --- data model --------------------------------------------------------------

Severity = Literal["hard", "warn"]
"""``hard`` — owns-vs-owns, refuse without ``--force``.

``warn`` — any overlap involving ``modifies``; refuse without ``--force``
but render the report differently (yellow, not red).
"""

ConflictKind = Literal["owns-vs-owns", "owns-vs-modifies", "modifies-vs-modifies"]


class ParcelClaims(BaseModel):
    """Structured scope declaration parsed from a parcel's YAML frontmatter.

    Unknown keys are ignored (pydantic default) so the schema can grow
    without breaking older parcels. Every list defaults to empty, so a
    parcel declaring only ``id`` is valid and simply participates in no
    conflict checks.
    """

    id: str
    priority: str = ""
    lane: str = ""
    owns: list[str] = Field(default_factory=list)
    modifies: list[str] = Field(default_factory=list)
    reads: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Overlap:
    """Single (our-pattern, their-pattern) pair that intersects."""

    our_pattern: str
    their_pattern: str
    severity: Severity
    kind: ConflictKind


@dataclass
class Conflict:
    """All overlaps between the candidate parcel and one specific other parcel."""

    other_parcel_id: str
    overlaps: list[Overlap] = field(default_factory=list)

    @property
    def has_hard(self) -> bool:
        return any(o.severity == "hard" for o in self.overlaps)


@dataclass
class ConflictReport:
    """Aggregate of every parcel-pair conflict found for the candidate."""

    parcel_id: str
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def has_hard(self) -> bool:
        return any(c.has_hard for c in self.conflicts)

    @property
    def has_any(self) -> bool:
        return bool(self.conflicts)


# --- frontmatter parser ------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, object] | None:
    """Extract the YAML frontmatter block from *text*, or ``None`` if absent.

    Raises :class:`ValueError` on malformed YAML so the caller can surface
    the filename alongside the parser error — a silent skip would let a
    typo'd frontmatter block disable scope checks on the offending parcel.
    """

    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        return None
    return data


def load_claims(markdown_path: Path) -> ParcelClaims | None:
    """Parse a parcel markdown file into :class:`ParcelClaims`.

    Returns ``None`` when the file is missing, unreadable, or lacks a
    frontmatter block. Raises :class:`ValueError` when frontmatter is
    present but malformed.
    """

    try:
        text = markdown_path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm = parse_frontmatter(text)
    if fm is None:
        return None
    fm.setdefault("id", markdown_path.stem)
    try:
        return ParcelClaims.model_validate(fm)
    except Exception as exc:  # pydantic ValidationError subclasses ValueError
        raise ValueError(f"invalid frontmatter in {markdown_path}: {exc}") from exc


# --- pattern matching --------------------------------------------------------

_GLOB_CHARS = frozenset("*?[")


def _normalize(pattern: str) -> str:
    """Forward slashes, strip leading ``./``. Trailing ``/`` is preserved."""
    p = pattern.strip().replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _is_glob(pattern: str) -> bool:
    return any(c in _GLOB_CHARS for c in pattern)


def _expand_directory(pattern: str) -> str:
    """``foo/`` → ``foo/**`` so directory claims match their contents."""
    if pattern.endswith("/"):
        return pattern + "**"
    return pattern


def _glob_matches(pattern: str, concrete: str) -> bool:
    """Does *concrete* (non-glob path) match *pattern* (glob)?"""
    try:
        if "**" in pattern:
            return PurePosixPath(concrete).match(pattern)
        return fnmatch.fnmatchcase(concrete, pattern)
    except ValueError:
        return False


def _path_is_prefix(parent: str, child: str) -> bool:
    """Is *parent* a directory prefix of *child* (or equal)?"""
    if parent == child:
        return True
    if not parent:
        return True
    return child.startswith(parent.rstrip("/") + "/")


def _glob_stem(pattern: str) -> str:
    """Literal prefix of a glob (path before the first glob char)."""
    stop = len(pattern)
    for i, ch in enumerate(pattern):
        if ch in _GLOB_CHARS:
            stop = i
            break
    stem = pattern[:stop]
    stem = stem.rsplit("/", 1)[0] + "/" if "/" in stem else ""
    return stem


def patterns_overlap(a: str, b: str) -> bool:
    """Does claim *a* overlap with claim *b*?

    Implemented as pattern-vs-pattern intersection (no filesystem lookup
    required), which lets the check fire on brand-new files the parcel
    will create but that don't yet exist on disk. Rules:

    - Equal patterns → overlap.
    - Neither is a glob → overlap only when one is a directory prefix of
      the other (``src/pkg/`` vs ``src/pkg/cli.py``).
    - One is a glob, the other a literal path → the glob matches the
      literal iff :func:`fnmatch.fnmatchcase` / :meth:`PurePosixPath.match`
      says so.
    - Both are globs → conservative stem comparison: if one literal stem
      is a prefix of the other's, they're reported as overlapping. False
      positives here are acceptable; false negatives are not.
    """

    a_n = _normalize(a)
    b_n = _normalize(b)
    if a_n == b_n:
        return True

    a_ex = _expand_directory(a_n)
    b_ex = _expand_directory(b_n)

    a_glob = _is_glob(a_ex)
    b_glob = _is_glob(b_ex)

    if not a_glob and not b_glob:
        return _path_is_prefix(a_n, b_n) or _path_is_prefix(b_n, a_n)

    if a_glob and not b_glob:
        return _glob_matches(a_ex, b_n)
    if b_glob and not a_glob:
        return _glob_matches(b_ex, a_n)

    sa = _glob_stem(a_ex)
    sb = _glob_stem(b_ex)
    if not sa or not sb:
        return True
    return sa.startswith(sb) or sb.startswith(sa)


# --- conflict detection ------------------------------------------------------


def _pair_overlaps(
    mine: list[str],
    theirs: list[str],
    severity: Severity,
    kind: ConflictKind,
) -> list[Overlap]:
    out: list[Overlap] = []
    for m in mine:
        for t in theirs:
            if patterns_overlap(m, t):
                out.append(
                    Overlap(
                        our_pattern=m,
                        their_pattern=t,
                        severity=severity,
                        kind=kind,
                    )
                )
    return out


def check_pair(a: ParcelClaims, b: ParcelClaims) -> Conflict | None:
    """Compute the overlap set between parcel *a* and parcel *b*.

    Returns ``None`` when no overlap exists; otherwise a :class:`Conflict`
    describing each matched (our-pattern, their-pattern) pair. ``reads``
    and ``forbidden`` are ignored here — only ``owns`` and ``modifies``
    participate in conflict semantics.
    """

    overlaps: list[Overlap] = []
    overlaps.extend(_pair_overlaps(a.owns, b.owns, "hard", "owns-vs-owns"))
    overlaps.extend(_pair_overlaps(a.owns, b.modifies, "warn", "owns-vs-modifies"))
    overlaps.extend(_pair_overlaps(a.modifies, b.owns, "warn", "owns-vs-modifies"))
    overlaps.extend(
        _pair_overlaps(a.modifies, b.modifies, "warn", "modifies-vs-modifies")
    )
    if not overlaps:
        return None
    return Conflict(other_parcel_id=b.id, overlaps=overlaps)


def detect_conflicts(
    target: ParcelClaims, others: Iterable[ParcelClaims]
) -> ConflictReport:
    """Detect overlaps between *target* and every parcel in *others*.

    Self-matches (same ``id``) are skipped so repeatedly enqueuing the
    same parcel id doesn't falsely report a conflict with itself.
    """

    report = ConflictReport(parcel_id=target.id)
    for other in others:
        if other.id == target.id:
            continue
        c = check_pair(target, other)
        if c is not None:
            report.conflicts.append(c)
    return report


__all__ = [
    "Conflict",
    "ConflictKind",
    "ConflictReport",
    "Overlap",
    "ParcelClaims",
    "Severity",
    "check_pair",
    "detect_conflicts",
    "load_claims",
    "parse_frontmatter",
    "patterns_overlap",
]
