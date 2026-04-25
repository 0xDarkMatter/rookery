---
name: parcel-reader
description: "Parse a parcel's PARCEL_DONE.md into structured sections (summary, what-built, contracts-honoured, deviations, open-questions, verification-results, affected-trees) without reformatting the body. Read-only utility used by parcel-audit and wave-triage. Triggers on: read PARCEL_DONE, parse parcel done, what did this parcel deliver, extract parcel report, parcel completion summary, fleet parcel report."
allowed-tools: "Read, Glob, Grep"
---

# Parcel Reader

Bundled with **claude-fleet** and works against any claude-fleet project — the `PARCEL_DONE.md` shape this skill parses is the public completion contract documented in claude-fleet's README, and the skill pairs with `claude-fleetd` (the orchestrator daemon) plus the rest of the parcel-* family (`parcel-audit`, `wave-triage`, `parcel-roster`). Originally extracted from Axiom but no longer Axiom-specific.

The canonical way to turn a `PARCEL_DONE.md` into labelled sections a caller can reason about. This skill does not summarise, rewrite, or score — it locates the file, identifies the standard sections by their headers, and hands each section back verbatim inside a structured block. Use it as a sub-step inside larger rituals (audit, wave-triage) whenever a parcel's own report is the source of truth.

## When to use

- The user asks what a parcel delivered, stubbed, or flagged
- Another skill needs the structured contents of a `PARCEL_DONE.md` (`parcel-audit`, `wave-triage`)
- Cross-checking an audit finding against the parcel's own claim
- Building a wave-wide status table from many parcels' reports

## When NOT to use

- The parcel has no `PARCEL_DONE.md` yet — report "absent" and exit; do not fabricate
- The file is a parcel *spec* (prompt), not a completion report — wrong input
- The caller wants a scored verdict — that is `parcel-audit`'s job

## Workflow

### Step 1 — Locate the file

Given a parcel id, resolve the worktree path via Glob. `<worktrees_root>` is the worktrees root directory configured in `claude-fleet.yaml` (default `./worktrees`):

```
<worktrees_root>/<id>/PARCEL_DONE.md
<worktrees_root>/<id>-*/PARCEL_DONE.md
```

The second form covers ids that expand to a suffixed directory (e.g. `I1` → `I1-merge`). If both shapes match, prefer the exact match. If neither matches, emit:

```
STATUS: absent
PARCEL: <id>
```

and stop. Absence of the file is a valid answer.

### Step 2 — Read the file

Use `Read` in full. Do not truncate. `PARCEL_DONE.md` files grow over audit iterations (iter-2, iter-3 sections appended); the tail is often the most current claim.

### Step 3 — Identify canonical sections

PARCEL_DONE reports are markdown with `##` section headers. Recognise these canonical names (match case-insensitively, allow small variants like "What built" vs "What was built"):

| Canonical label | Header variants |
|---|---|
| `summary` | Summary, Overview, What built, What was built, Notable design decisions |
| `outputs` | Outputs, Files produced, Artefacts, Deliverables, What shipped, Branches merged (in order) |
| `contracts-honoured` | Contracts honoured, Contracts, Interface contracts, Hard rules honoured |
| `deviations` | Deviations, Deviations from spec, Differences, Reconciliations performed |
| `stubs` | Stubs, Stubs / open questions, Known stubs, Known limitations, Deferred (per spec scope constraints) |
| `open-questions` | Open questions, Open items, Follow-ups, Known limitations / open items for \<lane\>, Deferred / open items for future parcels (incl. qualified forms like "Open items for follow-up") |
| `verification` | Done-criteria checklist, Final gate results, Commands to verify locally, Verification, Verification-results, Tests, Dogfood, Gate \<id\> — checked |
| `affected-trees` | Affected trees, Scope owned, Directory ownership, What I did NOT touch (inverse form — capture as affected-trees with an `inverse: true` annotation) |
| `iterations` | Iteration 2, Iteration 3, Iter-2, Iter-3, "Iteration \<N\> — Audit fixes" (one entry per iter block) |
| `commits` | Commit range (this branch), Commits on main, Commits landed |
| `items` | Per-item subsections matching `^##\s+[WPIG][0-9]+\s+—` — capture as an ordered list, each with its own verbatim body |

For each section present, record its header text verbatim, its start line, and its body (the markdown between this header and the next `##` or EOF). Unknown sections are captured under a residual `other` list with their literal header.

### Step 4 — Emit the structured block

Return a single fenced block that downstream skills can parse. Use the labels from step 3 as keys; emit each section's body verbatim inside its own fenced sub-block. Example shape:

```
PARCEL: <id>
PATH: <worktrees_root>/<id>/PARCEL_DONE.md
STATUS: present

## summary (header: "Summary")
<verbatim body>

## contracts-honoured (header: "Contracts honoured")
<verbatim body>

## open-questions (header: "Open items for follow-up")
<verbatim body>

## verification (header: "Final gate results")
<verbatim body>

## iterations
- iter-2: <verbatim body>
- iter-3: <verbatim body>

## other
- "Commands to verify locally" (line 65): <verbatim body>
```

Sections not present in the file are omitted from the block — do not emit empty placeholders. Do not add commentary between sections.

### Step 5 — Graceful degradation

- File absent → `STATUS: absent`, no sections
- File present but empty → `STATUS: empty`, no sections
- File has no `##` headers → treat the whole body as `summary`
- Malformed markdown (unclosed code fence) → still emit sections by header; note `WARN: unclosed fence near line N` in the block header

## Hard rules

| Rule | Why |
|---|---|
| Read-only; never write to the worktree | Parsing must not mutate the artefact it inspects |
| Emit section bodies verbatim, not summarised | Downstream skills may need exact wording (test counts, commit hashes, file paths) |
| Preserve the author's header text alongside the canonical label | Auditors need to know the real header to quote it back |
| Do not invent sections that are not in the file | Absence is a valid signal; invention corrupts downstream reasoning |
| Never extend the search outside the configured `<worktrees_root>` | That directory is the only canonical home for parcel worktrees in a claude-fleet project |

## Anti-patterns

| Anti-pattern | Why it hurts |
|---|---|
| Collapsing "Stubs" and "Open questions" into one label when both are present | Loses the author's intent to separate "I knew I stubbed this" from "I don't know the answer" |
| Paraphrasing "all tests green" when the body says "414 passed, 0 failed, 2 warnings" | Downstream audit needs the real numbers |
| Dropping the iteration history (`Iteration 2`, `Iteration 3`) because it "looks like commentary" | Iteration blocks are the audit-feedback trail; they are primary data |
| Returning prose ("Parcel X looks good") instead of the structured block | Breaks machine callers (`parcel-audit`, `wave-triage`) |
| Searching `.claude/worktrees/` for parcels | Wrong tree — those are lane worktrees, not parcel worktrees |

## See also

- `parcel-audit` — consumes this skill's output to produce a verdict
- `wave-triage` — aggregates many parcels' structured blocks into a wave-wide report
- `parcel-generate` — authors the parcel prompts whose done-reports this skill parses
- `~/.claude/rules/worktree-boundaries.md` — the `.claude/worktrees/` hands-off rule

