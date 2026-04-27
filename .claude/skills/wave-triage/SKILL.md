---
name: wave-triage
description: "Given the parcels belonging to a wave, compute merge order and gate status by combining parcel-roster inventory, parcel-reader claims, and parcel-audit verdicts against the wave's plan document. Surfaces: ready-to-merge, blocked, at-risk, and the next recommended merge. Triggers on: wave triage, wave status, what merges next, gate status, which parcels are blocked, wave readiness, merge order, wave plan."
allowed-tools: "Read, Bash, Glob, Grep"
---

# Wave Triage

The end-to-end wave-level decision skill. Aggregates per-parcel signals into a single sequenced report that says: *these parcels are ready to merge now, these are blocked and why, these are at-risk, and the next safe merge is X*.

Bundled with **rookery**. Pairs with `rookery status` (live fleet view), `rookery land history` (what has actually landed on main), and `rookery worktree list` (raw worktree listing). This skill is the decision-grade narrative layer above those CLIs.

Treats the wave's plan document as the source of truth for dep-order and gate definitions.

## When to use

- After a wave of parallel parcels has settled (most have written PARCEL_DONE.md)
- Before a merge pass, to confirm ordering is safe
- When a gate (per the wave plan) looks blocked and the user needs to know why
- Mid-wave to decide whether to spawn follow-up parcels (G-parcels) to unblock

## When NOT to use

- You want a single-parcel verdict — use `parcel-audit`
- You want to know what worktrees exist at all — use `parcel-roster`
- You want to change merge policy — this skill reports; the lane owner decides

## Path resolution (read this once per session)

This skill reads audits and worktree state under a configurable root. Resolve `<worktrees_root>` once, in this order:

1. **`rookery.yaml` in CWD**: read the `worktrees_root` key (top-level). Use this if present and non-empty.
2. **`ROOKERY_WORKTREES` env var**: fall back to this if step 1 yielded nothing.
3. **Default**: `./worktrees` (relative to CWD).

Resolve the **project repo root** with `git rev-parse --show-toplevel` (run from CWD) — used for `git -C <repo> log --grep=...` lookups against `main`.

Audits live at `<worktrees_root>/audits/`.

## Workflow

### Step 1 — Identify the wave and its plan

Accept a wave id (e.g. `wave-3`) or a parcel-id list. If a wave id is given, Glob from `<repo>` for its plan:

```
docs/WAVE<N>_PLAN.md
docs/wave-<N>-plan.md
docs/waves/<N>.md
```

Read the plan in full. Extract:

- **Parcel table**: the canonical list of parcels in the wave with their scope (typically in a §2 or §Plan table)
- **Dep graph**: each parcel's "depends on" column
- **Gates**: gate definitions with their criteria (typically §Go/No-Go or §Gates)
- **Parallelism plan**: the ASCII timeline or dep diagram showing which parcels can run concurrently

If no plan file exists, proceed with just the parcel-id list — but note `PLAN: absent` in the report; merge-order recommendations will be advisory only.

### Step 2 — Invoke parcel-roster

Use `parcel-roster` to get the fleet snapshot. Filter to the parcels in this wave (skip others). Note any parcels from the plan whose worktree is missing — they never started.

### Step 3 — Per-parcel deep signal

For each parcel in the wave, gather three signals:

1. **Completion claim** — invoke `parcel-reader` on its PARCEL_DONE.md. Record whether present, what the summary says, any open-questions or stubs flagged.
2. **Audit verdict** — Glob `<worktrees_root>/audits/<id>.v*.md`, pick the highest version, Read it, extract the verdict line. If no audit exists, record `AUDIT: absent`.
3. **Git state** — ahead/behind/dirty counts from step 2's roster output.

Do not re-run the dev loop — that is `parcel-audit`'s remit and is expensive. Trust the audit verdict, flag missing audits.

### Step 4 — Compute gate status

For each gate in the plan, evaluate its criteria against step 3's signals. A gate is:

- **open**: all criteria met — downstream parcels may proceed
- **blocked**: one or more criteria unmet — list which and why
- **partial**: some criteria met, others in flight — useful during mid-wave triage

Do not relax criteria to make a gate pass. The plan's criteria are authoritative.

### Step 5 — Compute merge order

Using the dep graph from step 1 and verdicts from step 3:

- A parcel is **ready-to-merge** if: audit verdict is PASS or PASS_WITH_WARNINGS, all deps are already merged (check `git -C <repo> log --oneline --grep="<dep-id>"`), no scope violations flagged
- A parcel is **blocked** if: audit verdict is FAIL, or a dep is not merged, or its PARCEL_DONE is absent
- A parcel is **at-risk** if: dirty worktree, or PARCEL_DONE present but audit missing, or audit is stale relative to commits (audit older than last commit)

Emit the next recommended merge as the ready-to-merge parcel with the fewest downstream dependents waiting on it (critical-path first).

### Step 6 — Emit the structured report

```
WAVE: <wave-id>
PLAN: <path or "absent">
EVALUATED-AT: <ISO timestamp>

## Gate status

| Gate | Status | Blocked-by |
|---|---|---|
| G1 | open | — |
| G2 | blocked | I2 PARCEL_DONE absent |
| G3 | partial | I3 audit in progress |

## Ready to merge (in order)

1. <id> — <verdict> — <one-line rationale>
2. <id> — <verdict> — <one-line rationale>

## Blocked

| id | verdict | blocker |
|---|---|---|
| ... | FAIL | 3 mypy errors in ... |

## At-risk

| id | issue | remediation |
|---|---|---|
| ... | audit stale (3 commits since v1) | re-run parcel-audit |

## Missing from the plan

| id (expected) | status |
|---|---|
| ... | worktree absent — never started |

## Next recommended action

<one sentence: e.g. "Merge parcel X; it unblocks the longest downstream chain.">
```

### Step 7 — Do not act

The skill reports. Merging, re-auditing, and authoring follow-up parcels are other concerns. The report is the product; hand-off is implicit. Operators can cross-check against `rookery land history` to see what has already landed.

## Hard rules

| Rule | Why |
|---|---|
| Plan document is authoritative for dep-order and gate criteria | Single source of truth; prevents drift between sessions |
| Never relax a gate criterion to make it pass | Gates exist to catch what parcels miss |
| Trust `parcel-audit` verdicts; do not re-verify | Audits are already expensive; triage is cheap by design |
| Report only; never merge, re-audit, or re-author | Triage is a decision-aid, not an action |
| Use `parcel-roster` + `parcel-reader` — do not duplicate their logic | Single source of truth for inventory and parsing |
| Resolve `<worktrees_root>` from config, never hardcode | Different deployments use different roots |

## Anti-patterns

| Anti-pattern | Why it hurts |
|---|---|
| Inferring a gate's criteria from the parcel list when the plan is present | The plan is the spec; inference is hallucination |
| Declaring a parcel ready-to-merge with an absent audit | Audit is the sign-off — absence is a hard block, not a warning |
| Sorting the ready-to-merge list by parcel id instead of by downstream-impact | Critical-path first minimises wall-clock; id sort is arbitrary |
| Collapsing `blocked` and `at-risk` into one bucket | They need different follow-ups; merging them loses signal |
| Invoking `parcel-audit` from this skill | Audits are expensive; if one is missing the answer is "ask the auditor" not "do it here" |

## See also

- `parcel-roster` — fleet-wide inventory; step 2 input
- `parcel-reader` — PARCEL_DONE parsing; step 3 input
- `parcel-audit` — verdict source; step 3 input
- `parcel-generate` — authors the specs whose gates this skill evaluates against
- `rookery status` / `rookery land history` / `rookery worktree list` — CLI counterparts

## Note on multi-role context

Wave triage assumes a wave is a coordinated batch of parcels with a written plan and gates. In a single-role / single-session workflow this collapses to "audit each parcel and decide which lands first" — most of step 4 (gate status) becomes degenerate. Skip the gate table in that case and emit only the **Ready to merge / Blocked / At-risk** sections; the rest of the workflow still applies.
