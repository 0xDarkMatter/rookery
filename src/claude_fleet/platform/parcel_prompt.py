"""Resolve a parcel id to its prompt markdown file.

Every launch surface needs the same lookup: given a parcel id like
``my-feature`` or ``P1``, find ``<id>.md`` somewhere under
``parcels/`` (but never under ``parcels/**/templates/``).

Kept separate from :mod:`claude_fleet.platform.worktree_dir` because its
concern is *files*, not the git worktree that hosts them.
"""

from __future__ import annotations

from pathlib import Path


def find_parcel_prompt(parcels_dir: Path, name: str) -> Path:
    """Find ``<name>.md`` under *parcels_dir*, excluding ``templates/``.

    Returns the shallowest match (fewest path components) so a top-level
    ``parcels/P1.md`` beats ``parcels/waves/P1.md``. Matches the
    ``find … | head -1`` ordering the retired bash launchers used.

    Raises
    ------
    FileNotFoundError
        When no match is found. Message includes the equivalent ``find``
        command for human diagnosis.
    """
    matches = [
        p for p in parcels_dir.rglob(f"{name}.md")
        if "templates" not in p.parts
    ]
    if not matches:
        raise FileNotFoundError(
            f"prompt not found for {name!r} under {parcels_dir}/ "
            f"(search: find parcels -type f -name '{name}.md' -not -path '*/templates/*')"
        )
    return sorted(matches, key=lambda p: len(p.parts))[0]


__all__ = ["find_parcel_prompt"]
