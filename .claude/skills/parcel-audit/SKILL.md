---
name: parcel-audit
description: "Audit a completed parcel against its spec. Reads the parcel prompt, reads PARCEL_DONE.md (via parcel-reader), runs the git diff triad, reads changed src files, executes the dev loop (uv sync + pytest + mypy + ruff), and writes a versioned audit at <worktrees_root>/audits/<id>.v<N>.md with a PASS / PASS_WITH_WARNINGS / FAIL verdict. Triggers on: audit parcel, review parcel, did this parcel deliver, parcel verdict, inspect PARCEL_DONE, parcel review."
allowed-tools: "Read, Bash, Glob, Grep, Write"
---

# Parcel Audit

The canonical post-build review of a single parcel. Produces a verdict, a findings table with file:line evidence, and a versioned audit file. Does not merge. Does not edit parcel source. Writes exactly one file: the audit.

Bundled with **claude-fleet**. Pairs with `claude-fleet status`, `claude-fleet land history`, and `claude-fleet worktree list` for fleet-level visibility around the audit.

## When to use

- A parcel has written its `PARCEL_DONE.md` and needs independent review before merge
- A prior audit exists (v1, v2, …) and the parcel has added commits since — produce a new version
- You need authoritative evidence for a gate decision in `wave-triage`

## When NOT to use

- The parcel has no `PARCEL_DONE.md` yet — run `parcel-roster` to confirm, then ask the parcel session to complete
- You only need a presence snapshot — use `parcel-roster`
- You want a multi-parcel merge-order decision — use `wave-triage`

## Path resolution (read this once per session)

This skill operates on parcel worktrees that live under a configurable root. Resolve `<worktrees_root>` once, in this order:

1. **`claude-fleet.yaml` in CWD**: read the `worktrees_root` key (top-level). Use this if present and non-empty.
2. **`CLAUDE_FLEET_WORKTREES` env var**: fall back to this if step 1 yielded nothing.
3. **Default**: `./worktrees` (relative to CWD).

Resolve the **project repo root** with `git rev-parse --show-toplevel` (run from CWD) — this is the path used by `uv -C <repo>` and for reading the spec.

Audits live at `<worktrees_root>/audits/` (a sibling directory under the worktrees root, *not* inside any worktree).

## Workflow

### Step 1 — Locate the spec and the worktree

Resolve `<worktrees_root>` and `<repo>` per the path-resolution block above.

Given a parcel id, Glob from `<repo>` for the spec in both known locations:

```
parcels/<id>.md
parcels/waves/*/<id>.md
parcels/waves/*/<id>-*.md
```

If more than one matches, prefer the most recently modified (parcel prompts sometimes get revised mid-wave). If none match, record `SPEC: absent` in the audit and continue — some parcels are ad-hoc and have no spec file.

Locate the worktree at `<worktrees_root>/<id>/` or `<worktrees_root>/<id>-*/` via Glob. If the worktree is missing, stop and emit `STATUS: worktree absent`.

### Step 2 — Read the PARCEL_DONE (via parcel-reader)

Invoke the `parcel-reader` skill on the worktree's `PARCEL_DONE.md`. Use its structured output as the parcel's own claim. Do not re-parse.

If `parcel-reader` returns `STATUS: absent`, stop and emit `STATUS: PARCEL_DONE absent` in the audit. Audit cannot proceed without a completion claim.

### Step 3 — Git triad

From the worktree:

```bash
git -C <wt> log --oneline main..HEAD
git -C <wt> diff --stat main
git -C <wt> status --short
git -C <wt> merge-base main HEAD
```

Record: commit count ahead of main, files touched, line deltas, dirty files, merge-base hash. A dirty worktree is a warning (not a fail) — the parcel may have legitimately uncommitted drafts.

### Step 4 — Scope check

Cross-reference the `git diff --stat main` output against the parcel's declared owned directories (from the spec's ownership table, or from the PARCEL_DONE's "affected-trees" section). Any file outside the owned dirs is a **scope violation** — record each with file:line evidence from the diff.

### Step 5 — Read changed source

For each file in `git diff --stat main`, Read the file in full from the worktree. Note:

- Public API shape (exported functions, classes, CLI commands)
- Contracts with other parcels (imports across module boundaries)
- Stubs or `TODO(P<N>)` markers that signal deferred work
- Tests: presence, coverage shape, any `@pytest.mark.skip` with a reason

Do not read files outside the diff — they are not the parcel's output.

### Step 6 — Dev loop

Run in the worktree (use the worktree's own module layout to scope):

```bash
uv -C <wt> sync --extra dev
uv -C <wt> run --extra dev python -m pytest tests/unit/<module> -v
uv -C <wt> run --extra dev python -m mypy src/<package>/<module> --strict
uv -C <wt> run --extra dev python -m ruff check src/<package>/<module>
```

Capture exit codes and the last ~30 lines of each. A test suite that errors at collection time is a FAIL. Warnings from ruff or mypy without errors are PASS_WITH_WARNINGS. All-green is PASS (modulo earlier steps).

### Step 7 — Verdict + findings table

Combine the signals:

| Signal | PASS | PASS_WITH_WARNINGS | FAIL |
|---|---|---|---|
| Scope violations | 0 | 0 | ≥1 |
| Dirty worktree | 0 | ≤5 | >5 or uncommitted tests |
| Tests (pytest) | green | warnings only | any failure or collection error |
| Types (mypy --strict) | clean | warnings only | any error |
| Lint (ruff) | clean | warnings only | any error |
| PARCEL_DONE claims match reality | yes | minor drift | material false claims |

Overall verdict is the worst signal. Build a findings table with one row per issue, citing `file:line` where applicable.

### Step 8 — Write the audit

Determine the version: Glob `<worktrees_root>/audits/<id>.v*.md`, pick max + 1 (or `v1` if none).

Write to `<worktrees_root>/audits/<id>.v<N>.md` using the canonical **Phase A–E** layout. The `<id>.v<N>.md` versioning convention is a public contract — keep it. Reviewers cross-read across the fleet and a divergent shape costs them.

```
# Audit: <id> (iter <N>)

**Verdict:** <PASS | PASS_WITH_WARNINGS | FAIL>

**Summary:** <2–4 sentence prose summary — what changed since any prior audit, current gate status, verdict rationale>

## Phase A — Scope

- Files changed: **N** (`+L / -L`)
- Out-of-scope writes: **none** (or: list each with evidence)
- Deviations from declared owned dirs (per the spec's ownership table or AGENTS.md): <list with file:line>

## Phase B — Contracts

<interfaces / schemas / public API / imports honoured vs drifted; cite commit hashes or file:line>

## Phase C — Tests / Types / Lint

- `pytest tests/unit/<module>` — <summary + count>
- `pytest tests/integration/<module>` — <summary + count>
- `mypy src/<package>/<module> --strict` — <summary>
- `ruff check src/<package>/<module>` — <summary>

## Phase D — Quality

<non-gate observations: documentation coverage, error messages, logging, test isolation, stub markers, technical debt the parcel chose to ship>

## Phase E — Integration Risks (for the next integrator)

<flag anything that will bite at merge: shared files with other parcels, implicit deps on unfinished work, migrations that conflict>

## Recommendations

<ranked list of concrete follow-ups: merge / merge-with-followup / block-on-fix / reject, plus specific patch-sized fixes>

## Raw artefacts

<tool output excerpts: last 20-30 lines of pytest / mypy / ruff, verbatim>
```

Commit the audit (on `main`, via the project repo) with a message like `audit(parcel/<id>): v<N> <verdict>`. The audit lives at `<worktrees_root>/audits/<id>.v<N>.md` — the parcel's own branch does not track audits.

## Hard rules

| Rule | Why |
|---|---|
| Never edit parcel source | Audit is read-only on the subject; one write allowed — the audit file |
| Never merge, cherry-pick, or push | Merge is a separate (git-ops) concern; audit precedes it |
| Audit file goes to `<worktrees_root>/audits/`, NOT inside the worktree | Project convention; keeps worktree diffs clean |
| Use `git -C <wt>` and `uv -C <wt>`, never `cd` | Maintains permission grants; project bash convention |
| Cite evidence with `file:line` | Reviewers must be able to verify findings |
| Never fabricate test numbers | If pytest fails at collection, say so; do not invent pass-counts |

## Anti-patterns

| Anti-pattern | Why it hurts |
|---|---|
| Using `ls` / `find` / `cat` via Bash for file ops | Grep/Glob/Read are the dedicated tools |
| Paraphrasing PARCEL_DONE instead of quoting verbatim | Reviewer needs the author's exact words to catch drift |
| Marking PASS when `mypy --strict` has errors but pytest is green | Strict mypy is a mandatory gate; warnings-only is the generous read |
| Running the dev loop without `--extra dev` | Dev deps are scoped to the extra; bare `uv sync` is a false-negative setup |
| Writing to the worktree's PARCEL_DONE.md | The parcel session owns that file; audits go to the audits dir |
| Reading files outside the diff "for context" | Expands scope, inflates audit, misses the point |
| Hardcoding a worktrees path | Always resolve via `claude-fleet.yaml` → `CLAUDE_FLEET_WORKTREES` → `./worktrees` |

## See also

- `parcel-reader` — parses the PARCEL_DONE this skill consumes
- `parcel-roster` — surface-level inventory; use before picking what to audit
- `wave-triage` — consumes audit verdicts to sequence merges
- `parcel-generate` — authors the spec this skill compares against
- `claude-fleet status` / `claude-fleet land history` / `claude-fleet worktree list` — fleet-level commands for orientation around the audit
