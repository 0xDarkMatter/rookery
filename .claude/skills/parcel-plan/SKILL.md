---
name: parcel-plan
description: "Decompose a spec document (architecture, roadmap, wave plan) into a ranked list of candidate parcels ready for parcel-generate. Given a goal and a spec, produce parcel IDs with scope, dependencies, estimated duration, and parallel-safe groupings — the upstream half of the parcel-generate pipeline. Triggers on: plan parcels, decompose spec, parcel breakdown, wave plan, parcel sequencing, what parcels do we need, spec to parcels, parcel decomposition, fleet wave plan."
allowed-tools: "Read, Glob, Grep"
---

# Parcel Plan

Bundled with **claude-fleet** and works against any claude-fleet project — the parcel format and worktree layout are documented in claude-fleet's README, and this skill pairs naturally with `claude-fleetd` (the orchestrator daemon) and the rest of the parcel-* family. It was originally extracted from the Axiom build but is no longer Axiom-specific.

The upstream companion to `parcel-generate`. Where `parcel-generate` takes a ready-made parcel breakdown and writes the prompt files, this skill takes a raw spec and produces the breakdown.

Output is a ranked list of candidate parcels, each with enough structure that `parcel-generate` can take it directly.

## When to use

- A new wave is being planned and the spec (PROJECT.md, WAVE3_PLAN.md, ROADMAP.md, etc.) needs to be turned into parcelable units
- An existing wave needs rebalancing (e.g. a parcel is too big and must be split, or two parcels share files and must be merged)
- Mid-build, a gap surfaces and you need to scope a follow-up G-parcel (gap-fill) or I-parcel (integration)
- Cross-checking an existing parcel list against the spec to find missing coverage

## When NOT to use

- The spec is too fluid to plan against — stabilise it first
- You have a clear parcel list already and just need the prompt files — use `parcel-generate` directly
- You want merge-order for in-flight parcels — use `wave-triage`
- The work is inherently sequential (serial parcels like P0 bootstrap, P12 integration) — document as an interactive step, not a parcel

## Workflow

### Step 1 — Read the source spec

Identify the canonical source. Common spec inputs in priority order (illustrative — adapt to the project's actual doc layout):

- `docs/WAVE<N>_PLAN.md` — if it exists, its §2 parcel table is often 80% of the answer and only needs gap-filling
- `docs/BUILD_PLAN.md` — phase-level structure; parcels live within phases
- `docs/PARCELS.md` — any previous wave's parcels (reference patterns, ownership tables)
- `docs/ROLES.md`, `docs/ARCHITECTURE.md` — if no wave plan exists, the architecture doc implies decomposition via its module boundaries

Read the spec in full. Do not skim — parcel boundaries live in sentences like "owns X", "depends on Y", "must not touch Z".

### Step 2 — Extract candidate parcel units

A candidate parcel is a unit of work that:

1. **Has an owner directory or module** — concrete scope boundary (e.g. `src/<pkg>/bus/`, `docs/admin/`)
2. **Has a measurable done criterion** — tests green, a CLI command works, a file exists
3. **Fits in 2–4 hours of wall-clock** — larger → split; smaller → merge
4. **Has at most 2 upstream dependencies** — more than that is a sign the decomposition is wrong

For each candidate, capture:

| Facet | Extracted from |
|---|---|
| ID | Convention: letter prefix (P for build, I for integration, G for gap-fill, W for wave-specific) + number |
| Name | Short imperative phrase ("merge-to-main", "bench-submission", "rule-hook") |
| Scope | Owned dirs/files — enumerate verbatim from the spec |
| Deps | Other parcel IDs that must land first |
| Duration | Your honest estimate in hours (1–4) |
| Parallel-safe | Which other parcels in the same wave it can run concurrently with (disjoint files) |

### Step 3 — Detect and fix scope collisions

Two parcels must not write to the same file. For every pair `(A, B)` in the candidate list, cross-reference their scope columns. If overlap exists:

- **Same file, different concerns** → one parcel becomes a dep of the other; sequence them
- **Same directory, non-overlapping files** → split the directory into sub-scopes (e.g. "`A` owns `foo/api.py`, `B` owns `foo/models.py`")
- **Same file, same concern** → merge A and B into one parcel

Flag unresolvable collisions and stop — a plan with overlap guarantees merge hell.

### Step 4 — Compute the dependency DAG

Construct the DAG from the `deps` columns. Detect and report:

- **Cycles** — unrecoverable; a parcel cannot depend on its transitive output. Fix the decomposition.
- **Orphans** — parcels no downstream parcel consumes; verify intentional (often the demo / submission / polish parcels sit here)
- **Bottlenecks** — parcels with ≥3 downstream dependents; these are critical-path and deserve extra budget

### Step 5 — Assign to waves

Greedy algorithm:

1. Wave 1 = all parcels with no deps
2. Wave N+1 = all parcels whose deps are fully satisfied by waves ≤ N
3. Repeat until all parcels are assigned

If a wave exceeds ~8 parcels, consider splitting along scope lines (e.g. infra parcels first, role parcels second) to keep the launch operator's mental model manageable.

### Step 6 — Emit the plan

Produce a structured report:

```
## Plan summary

GOAL: <one-line restatement of the wave's goal>
SPEC: <resolved path>
WAVES: <N>
PARCELS: <M>
CRITICAL-PATH: <ordered list of parcel IDs>
EST-WALL-CLOCK: <hours>, assuming <K>-parallel
DETECTED-COLLISIONS: <list or "none">
ORPHANS: <list or "none">

## Parcels

### Wave 1

| ID | Name | Scope | Deps | Est | Parallel-safe |
|---|---|---|---|---|---|
| ... | ... | ... | — | 2h | all wave-1 |

### Wave 2
...

## Open questions (for Planning lane to resolve before parcel-generate runs)

- <any ambiguity in the spec that prevents unambiguous parcel authoring>
```

### Step 7 — Hand-off

The output of this skill is the direct input to `parcel-generate` Step 1–2. Downstream operator runs `parcel-generate` to turn each row into a committed `parcels/<id>.md` prompt file.

Do not author the prompt files yourself — that is `parcel-generate`'s remit. Do not launch sessions — that is `dsp-launch` or `claude-fleet` (the queue-based dispatcher).

## Hard rules

| Rule | Why |
|---|---|
| Read the spec in full before decomposing | Parcel boundaries are often in paragraphs, not tables |
| Scope collisions are a hard error | Overlap at plan time is merge hell at build time |
| Every parcel must have a concrete done criterion | "Implement X" without a test / command / file is not a parcel |
| Never invent deps not implied by the spec | Fabricated deps create false sequencing and leave parallelism on the table |
| Estimate duration honestly, not optimistically | 4h-est parcels that take 8h break the wave plan; under-promise |

## Anti-patterns

| Anti-pattern | Why it hurts |
|---|---|
| Copying the spec's §2 table verbatim without re-checking scope | Specs drift; overlap creeps in; plan becomes a stale projection |
| Assigning every parcel to Wave 1 to "maximise parallelism" | Deps exist for a reason; ignoring them ships broken waves |
| Merging too-small parcels without a reason | Parcel size is a crash-recovery and context-budget trade-off; smaller is not always worse |
| Splitting a parcel into two that both need the same file | Scope collision; worse than a larger parcel |
| Using the skill to *author* parcel prompts | Wrong skill; this is a planning doc, not prompt content. Hand off to `parcel-generate`. |

## See also

- `parcel-generate` — consumes this skill's output; writes the prompt files
- `wave-triage` — evaluates gate status and merge order for in-flight waves this skill produced
- `parcel-roster` — inventories what was actually built vs what this skill planned
- The project repo root's spec docs (e.g. `docs/BUILD_PLAN.md`, `docs/WAVE<N>_PLAN.md`) — primary spec inputs
