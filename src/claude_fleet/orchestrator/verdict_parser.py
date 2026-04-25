"""Parse the final verdict out of an audit report written per ``parcels/meta/AUDIT.md``.

Absorbs the verdict-extraction logic previously embedded as an ``awk``
pipeline in :file:`scripts/auto-feedback-loop.sh`. Two surface forms are
accepted so the parser survives both the current corpus of audits
(``**Verdict:** PASS``, bold-markdown) and the cleaner ``VERDICT: PASS``
form the R2-5 parcel spec anticipates going forward.

Only the first well-formed verdict line wins — matching the bash
``awk '… {print $2; exit}'`` short-circuit. A report that accidentally
embeds a second ``Verdict:`` in a prose section (common in re-audit
narratives that quote the prior verdict) is never allowed to overwrite
the real one at the top of the file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

Verdict = Literal["PASS", "PASS_WITH_WARNINGS", "BLOCK"]

_VALID: frozenset[Verdict] = frozenset({"PASS", "PASS_WITH_WARNINGS", "BLOCK"})

# Matches ``**Verdict:** PASS`` / ``VERDICT: PASS`` at line start. Accepts a
# trailing ``ED`` (``PASS_WITH_WARNED``-style typos were observed historically
# in hand-edited audits; canonicalise below) — but only the canonical triple
# above is ever returned.
_LINE_RE = re.compile(
    r"^\s*\**\s*verdict\s*:\s*(?:\**\s*)*([A-Za-z_]+)",
    re.IGNORECASE,
)


def parse_audit_report(path: Path) -> Verdict:
    """Extract the final PASS / PASS_WITH_WARNINGS / BLOCK verdict from *path*.

    Scans top-to-bottom and returns the first recognisable verdict line.
    The scan order matches the bash implementation it replaces; the
    auditor prompt (``parcels/meta/AUDIT.md``) asks for a single verdict
    at the top of the report.

    Normalisations:
      * ``PASS_WITH_WARNING`` (singular) → ``PASS_WITH_WARNINGS`` — observed
        in ad-hoc audits; both bash and the R2-5 CLI should treat them as
        the same outcome.
      * Any casing / leading asterisks stripped.

    Raises:
        FileNotFoundError: *path* does not exist.
        ValueError: *path* exists but contains no recognisable verdict line,
            or the verdict token is not one of the three allowed values.
    """

    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        m = _LINE_RE.match(raw)
        if not m:
            continue
        token = m.group(1).upper().rstrip("*")
        if token == "PASS_WITH_WARNING":
            token = "PASS_WITH_WARNINGS"
        if token in _VALID:
            # token ∈ _VALID ⇒ token is a Verdict by construction; narrow explicitly.
            assert token in ("PASS", "PASS_WITH_WARNINGS", "BLOCK")
            return token
        raise ValueError(
            f"unrecognised verdict token {token!r} in {path} — "
            f"expected one of PASS / PASS_WITH_WARNINGS / BLOCK"
        )
    raise ValueError(f"no verdict line found in {path}")


__all__ = ["Verdict", "parse_audit_report"]
