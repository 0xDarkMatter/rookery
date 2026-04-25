"""Parcel scaffold and validation logic (G3).

Public API:
- parcel_new(id, prompt_path, force) -> Path
- parcel_validate(path) -> ValidationResult
- ValidationResult (Pydantic model)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class ValidationResult(BaseModel):
    ok: bool
    errors: list[str]
    warnings: list[str]


def parcel_new(
    id: str,
    prompt_path: Path | None = None,
    force: bool = False,
) -> Path:
    """Scaffold a new parcel markdown file.

    Args:
        id: Parcel identifier, used as filename stem and frontmatter id.
        prompt_path: Output path. Defaults to ``parcels/<id>.md``.
        force: If True, overwrite an existing file. If False (default),
               raise FileExistsError when the target already exists.

    Returns:
        The Path of the created file.

    Raises:
        FileExistsError: If the target exists and *force* is False.
    """
    target = prompt_path or Path(f"parcels/{id}.md")
    if target.exists() and not force:
        raise FileExistsError(target)

    template = f"""---
id: {id}
priority: 0
deps: []
max_attempts: 3
verification_enabled: true
verdict_adapter: marker-file
notes: ""
---

# {id}

You are working in a fresh git worktree. <describe the task here>

## Acceptance

- <criterion 1>
- <criterion 2>

## Verdict

When you finish, write a file at the worktree root named
`PARCEL_DONE-{id}.md` with this shape:

    # PARCEL_DONE: {id}

    Verdict: PASS

    ## Summary
    <one paragraph>

If you cannot complete the work, set `Verdict: BLOCK` and explain why.
"""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: list[str] = ["id"]

_FIELD_TYPES: dict[str, Any] = {
    "priority": int,
    "deps": list,
    "max_attempts": int,
    "verification_enabled": bool,
    "verdict_adapter": str,
    "notes": (str, type(None)),
}


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split ``---`` delimited YAML from body.

    Returns (frontmatter_str, body_str).  Raises ValueError if no frontmatter.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        raise ValueError("no YAML frontmatter found (missing opening '---')")

    end_index: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            end_index = i
            break

    if end_index is None:
        raise ValueError("unclosed YAML frontmatter (missing closing '---')")

    frontmatter = "".join(lines[1:end_index])
    body = "".join(lines[end_index + 1 :])
    return frontmatter, body


def parcel_validate(path: Path) -> ValidationResult:
    """Validate a parcel markdown file.

    Checks (errors block enqueue; warnings are advisory):
    - File exists and is readable
    - YAML frontmatter parses without error
    - Required field ``id`` is present
    - Optional fields match declared types when present
    - Frontmatter ``id`` matches the file's stem
    - Body is non-empty
    - Body references ``PARCEL_DONE-<id>.md`` (warn if missing)
    """
    errors: list[str] = []
    warnings: list[str] = []

    # 1. File existence / readability
    if not path.exists():
        errors.append(f"file not found: {path}")
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"cannot read file: {exc}")
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    # 2. Frontmatter parse
    try:
        frontmatter_str, body = _split_frontmatter(text)
    except ValueError as exc:
        errors.append(f"frontmatter parse error: {exc}")
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    try:
        fm: dict[str, Any] = yaml.safe_load(frontmatter_str) or {}
    except yaml.YAMLError as exc:
        errors.append(f"YAML parse error: {exc}")
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    if not isinstance(fm, dict):
        errors.append("frontmatter must be a YAML mapping")
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    # 3. Required fields
    for field in _REQUIRED_FIELDS:
        if field not in fm:
            errors.append(f"missing required field: {field!r}")

    # 4. Field type checks (only when field is present)
    for field, expected_type in _FIELD_TYPES.items():
        if field in fm:
            value = fm[field]
            if not isinstance(value, expected_type):
                if isinstance(expected_type, tuple):
                    type_names = " | ".join(t.__name__ for t in expected_type)
                else:
                    type_names = expected_type.__name__
                errors.append(
                    f"field {field!r} has wrong type: expected {type_names}, "
                    f"got {type(value).__name__}"
                )
            elif field == "deps":
                # Ensure every element is a string
                for i, item in enumerate(value):
                    if not isinstance(item, str):
                        errors.append(
                            f"field 'deps[{i}]' must be a string, got {type(item).__name__}"
                        )

    # 5. Frontmatter id matches filename stem
    if "id" in fm:
        parcel_id = fm["id"]
        if str(parcel_id) != path.stem:
            errors.append(
                f"frontmatter id {parcel_id!r} does not match filename stem {path.stem!r}"
            )

    # 6. Body non-empty
    if not body.strip():
        errors.append("parcel body is empty")

    # 7. PARCEL_DONE reference (warn only)
    if "id" in fm and body.strip():
        done_marker = f"PARCEL_DONE-{fm['id']}.md"
        if done_marker not in body:
            warnings.append(
                f"body does not reference {done_marker!r} — workers may not know "
                "where to write their verdict file"
            )

    ok = len(errors) == 0
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)


__all__ = ["ValidationResult", "parcel_new", "parcel_validate"]
