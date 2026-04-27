---
name: parcel-generate
description: "Generate self-contained headless-agent prompt files from a spec doc for DSP parallel build. Reads a PARCELS.md-style breakdown and writes one prompt per parcel that a fresh claude -p session can execute without inherited context. Triggers on: parcel prompts, headless prompts, generate parcels, spec-to-parcels, fan-out prompts, DSP prompts, parcel generation, write parcel, fleet parcel."
license: MIT
allowed-tools: "Read Write Edit Glob Grep Bash"
metadata:
  author: claude-mods
---

# parcel-generate

Bundled with **rookery** and works against any rookery project. The parcel prompt shape and `PARCEL_DONE.md` completion contract are documented in rookery's README; this skill pairs with `rookery-daemon` (the orchestrator daemon) and `dsp-launch` (file-based, pre-queue) for the launch step. Originally extracted from Axiom but no longer Axiom-specific.

Companion to the `dsp-launch` skill. Where `dsp-launch` covers the launch mechanics (worktrees, `claude -p`, watcher), this skill covers the upstream step: translating a project's per-parcel spec sections into self-contained prompt files that fresh headless sessions can execute.

## When to use

- A project's spec doc (`PARCELS.md`, `TASKS.md`, similar) decomposes the build into N parcels with explicit scope, inputs, outputs, contracts
- You need a prompt file per parcel for `claude -p` to consume
- Total wall-clock would justify parallelism (≥3 parcels, ≥3 hours serial)
- Max plan / OAuth is available so per-session cost is zero

## When NOT to use

- The spec is still fluid (contracts will churn → merge hell)
- Parcels share files (race on writes)
- The work is inherently sequential

## Procedure

### Step 1 — Extract the parcel index

Identify the source spec (commonly `docs/PARCELS.md`, but adapt to the project's layout). Confirm the dependency graph and parallel waves. Produce a list of parcel IDs + section references.

### Step 2 — Extract the canonical facets per parcel

For each parcel, lift from the spec:

- **Priority** — `P0` critical path / `P1` high-value / `P2` deferrable / `P3` optional (see legend below)
- **Lane affinity** — which lane primarily owns execution (building / wiring / admin / docs / skills-* / etc.)
- **Scope** — exact directories/files this parcel owns exclusively
- **Inputs** — docs and prior parcels it may read
- **Outputs** — files it must create
- **Contracts** — function/class signatures other parcels depend on (verbatim)
- **Tests** — what must be green before done
- **Done criteria** — explicit, checkable
- **Depends-on** — other parcels that must land first
- **Estimated duration** — for monitoring budgets

**Priority legend** (use these exact labels in the parcel header):

| Label | Meaning |
|---|---|
| `P0` | Critical path — blocks downstream parcels or release |
| `P1` | High value — meaningfully improves outcome or unblocks parallel work |
| `P2` | Useful, deferrable — doesn't block release |
| `P3` | Post-release / optional |

### Step 3 — Apply the canonical prompt template

Every parcel prompt follows the same structure. Fresh headless sessions see NO conversation history, so the prompt must be fully self-contained.

```markdown
# Parcel <ID> — <short name>

> Executable parcel prompt.
>
> **Priority**: `<P0|P1|P2|P3>` — <one-line rationale>
> **Lane affinity**: <building|wiring|admin|docs|skills-own|skills-software|planning>
> **Depends on**: <comma-separated parcel IDs, or "none">
> **Estimated duration**: <rough hours; omit per user preference if estimates unreliable>

## Context
<1-2 sentences on the overall project + what this parcel does.>

## Your scope (exclusive ownership)
- <dir 1>
- <dir 2>
Do NOT touch anything outside these paths.

## Read first
1. <HANDOFF / README>
2. <primary spec doc>
3. <your section in PARCELS.md>
4. AGENTS.md (conventions)

## Outputs (create these)
- <file> — <purpose>
- ...

## Contracts (frozen — other parcels depend on these signatures exactly)
```python
# verbatim signatures other parcels import
```

## Tests
- <test file> — <what it asserts>

## Done criteria (verify all before writing parcels/done/<PARCEL_ID>.md)
- [ ] outputs exist
- [ ] `uv run pytest tests/unit/<module>` green
- [ ] `uv run mypy src/<module> --strict` clean
- [ ] `uv run ruff check src/<module>` clean
- [ ] `parcels/done/<PARCEL_ID>.md` written and committed

## Hard rules
1. Never modify files outside your scope.
2. Commit progressively (every working function).
3. No real external API calls in tests — mock everything.
4. If you need a symbol from another parcel that isn't yet stable, STUB it.
5. Type hints + docstrings + pydantic v2 + structlog.
6. OAuth only — never set `ANTHROPIC_API_KEY`.
7. When done, write `parcels/done/<PARCEL_ID>.md` and exit.

## Workflow
You are in worktree `.` on branch `parcel/<ID>`. Run `uv sync` first.

Estimated duration: ~<N> hours.
```

### Step 4 — Enforce invariants during generation

Every prompt MUST assert:

- **Scope discipline** — never modify files outside owned dirs
- **Progressive commits** — not one giant end-of-parcel commit (crash-recovery depends on it)
- **OAuth only** — never `ANTHROPIC_API_KEY` (budget hygiene)
- **Mock external APIs in tests** — no surprise spend
- **`parcels/done/<PARCEL_ID>.md` is the sole completion signal** — absence = not done. Path MUST include the parcel ID (e.g. `A3`, `W9`, `G1`, `I2`) so it is globally unique across all lanes; the rolling `PARCEL_DONE.md` is forbidden (merge conflict risk). Path lives under `parcels/done/` so it is committable (root `.gitignore` excludes `/PARCEL_DONE-*.md`). The orchestrator's harvest also accepts the legacy `PARCEL_DONE-<PARCEL_ID>.md` at worktree root for backward compatibility, but new prompts MUST use the canonical `parcels/done/` path.
- **Stub unavailable cross-parcel symbols** — don't silently diverge from contracts
- **Fresh-session self-contained** — prompt must read standalone, no "remember when we discussed…"
- **Sandbox-aware for restricted paths** — if the parcel writes under `.claude/**` (or any path the default permission engine may deny), the prompt MUST include a probe-first preamble (`mkdir -p <target>/_probe && echo x > <target>/_probe/t && rm -rf <target>/_probe`). If the probe fails, the headless session exits immediately with a clear "sandbox deny — parent must route" signal, rather than burning context drafting content it cannot commit. See `AGENTS.md §7` on sub-agent write hygiene.
- **Draft-and-return for `.claude/**` targets** — when the sole deliverable lives under `.claude/**`, prefer a draft-and-return contract over direct-write: the headless session returns full file contents in its final message, and the parent session (which owns the permission context) writes them. This is the resilient pattern; direct-write under `.claude/**` from a sub-agent is the fragile one.

### Step 5 — Write files

Write each prompt to `parcels/waves/wave-<N>/<ID>.md` for wave-scoped work, or `parcels/meta/<ID>.md` / `parcels/adhoc/<ID>.md` for cross-wave utilities and one-offs. The active wave number comes from the current plan doc (e.g. `parcels/waves/wave-4/PLAN.md`, or ask the user if ambiguous).

Serial parcels (P0 bootstrap, P12 integration, P13 submission polish) still get prompt files in their wave dir, marked as interactive-only so the launcher skips them.

Launch scripts resolve parcels by id via `find parcels -name <ID>.md` (layout-agnostic), so the precise subdirectory doesn't affect invocation — pick the one that matches the parcel's lifecycle scope. Use `parcels/templates/new-parcel-template.md` as the skeleton.

### Step 6 — Verify

For every generated prompt, verify:
- Frontmatter / structure matches the template
- Contracts block contains verbatim signatures (not paraphrases)
- All "Read first" references are real files/sections
- Done criteria are objectively checkable
- Any stubs to other parcels are clearly noted
- A fresh reader with zero project context could start working

If a prompt fails verification, iterate on the source spec (not the prompt) — the source is authoritative.

## Anti-patterns

| Anti-pattern | Why it hurts |
|---|---|
| Prompt references "our earlier discussion" | Fresh session has none |
| Contracts paraphrased, not verbatim | Parcel A and parcel B implement different signatures → integration chaos |
| "When done, report back" with no `parcels/done/<PARCEL_ID>.md` | Monitor can't detect completion vs hang |
| Scope expressed as "mostly" or "generally" | Parcels stomp on each other |
| Tests deferred to end of parcel | Crash-recovery kills all uncommitted work |
| Prompt assumes a specific prior parcel already landed | Parallel launch makes this false |

## Output artefact

A `parcels/` directory with one `.md` file per parcel, each self-contained. Companion scripts/launchers live in `scripts/` (that's `dsp-launch`'s job, not this skill's).

## Integration with dsp-launch and rookery-daemon

This skill produces the input that `dsp-launch` and `rookery-daemon` consume:

```
parcel-generate  →  parcels/P1.md … P13.md
                              ↓
                       dsp-launch / rookery   →   <worktrees_root>/P{1..13}/
                              ↓
                       claude -p sessions run (or dispatched via rookery-daemon)
                              ↓
                       parcels/done/<PARCEL_ID>.md per worktree
```

`<worktrees_root>` is the worktrees root directory configured in `rookery.yaml` (default `./worktrees`).

## Notes

- If contracts in the source spec are incomplete, the best parcel prompt still fails — generation can't fix a bad spec. Flag the gap back to the spec author.
- Rough word budget per prompt: 800–1500 words. Longer bloats context; shorter omits context.
- Commit the prompts to the main branch BEFORE launching the wave, so respawned sessions pick up the same prompt.
- For fixed-deadline builds, cap total parcels at ~12 — more than that and integration debt dominates.
