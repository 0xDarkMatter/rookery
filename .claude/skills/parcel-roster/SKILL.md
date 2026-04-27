---
name: parcel-roster
description: "Inventory active parcel worktrees under the configured worktrees root and emit a table per parcel: branch, commits ahead/behind main, PARCEL_DONE presence, audit presence, last commit. The parcel analogue of lane-roster. Triggers on: parcel roster, which parcels, parcel status, list parcels, what parcels exist, parcels overview, parcel fleet, parcel inventory."
allowed-tools: "Read, Bash, Glob, Grep"
---

# Parcel Roster

The across-the-board view of active parcel worktrees. Answers: *what parcels are in flight, what state are they in, and which ones are ready for audit or merge?*

Bundled with **rookery**. Pairs with `rookery worktree list` (the lower-level CLI view) and `rookery status` / `rookery land history` (history-side views). This skill is the agent-friendly markdown analogue.

Mirrors `lane-roster` but targets the **parcel** tree (the configured worktrees root) rather than the **lane** tree (`.claude/worktrees/*`). The two are separate — do not conflate them.

## When to use

- Starting a wave-triage pass and needing a snapshot of the fleet
- Asking "what's ready to merge?" after a wave of parallel builds
- Deciding whether a parcel is still in flight, done but unaudited, or audited-and-waiting
- Before authoring a new parcel, to see which ids are already taken

## When NOT to use

- You want lane sessions (`.claude/worktrees/`) — use `lane-roster` instead
- You want detailed verdicts per parcel — use `parcel-audit`
- You want merge-order decisions — use `wave-triage`

## Path resolution (read this once per session)

This skill operates on parcel worktrees that live under a configurable root. Resolve `<worktrees_root>` once, in this order:

1. **`rookery.yaml` in CWD**: read the `worktrees_root` key (top-level). Use this if present and non-empty.
2. **`ROOKERY_WORKTREES` env var**: fall back to this if step 1 yielded nothing.
3. **Default**: `./worktrees` (relative to CWD).

The audits sibling lives at `<worktrees_root>/audits/`.

## Workflow

### Step 1 — Enumerate parcel worktrees

Resolve `<worktrees_root>` per the path-resolution block above. Iterate directory entries under `<worktrees_root>/` (Bash for-loop over `*/`, or Glob+filter). For each entry, include it only if **both** are true:

1. It is a directory (skip loose files — real fleets have `*.pid`, `*.iter`, and occasional log files at top level that are NOT worktrees)
2. It contains a `.git` file or directory (real worktrees always do; Glob alone can't confirm)

Special-case dirs to skip by name: `audits/`, `logs/`, `smoke-runs/`. These are sibling artefact roots, not worktrees. Extending this blacklist is cheap — prefer the `.git` presence check as the authoritative filter.

If the Glob returns nothing, emit:

```
STATUS: no parcel worktrees found
ROOT: <worktrees_root>
```

and stop. An empty tree is a valid answer.

### Step 2 — Per-worktree git state

For each candidate `<wt>`:

```bash
git -C <wt> branch --show-current
git -C <wt> log --oneline -1
git -C <wt> rev-list --count main..HEAD   # commits ahead
git -C <wt> rev-list --count HEAD..main   # commits behind
git -C <wt> status --porcelain | wc -l    # dirty count
git -C <wt> stash list | wc -l            # stash count
```

Use `git -C <path>` — never `cd`. If any command errors (e.g. the dir is not a git worktree, or main no longer exists), record the error verbatim in that row's error column and move on. Do not abort the whole roster on one bad worktree.

### Step 3 — Check completion + audit markers

For each `<wt>`, check file presence via Glob (not Bash `ls`):

- `<wt>/PARCEL_DONE.md` — the parcel's own completion report
- `<worktrees_root>/audits/<id>.v*.md` — audit files (zero or more; record the highest version present)

Where `<id>` is the worktree directory name.

### Step 4 — Emit the table

Produce a markdown table sorted by parcel id (alphanumeric, so `I1` < `I2` < `P1` < `P10` < `P2` unless a natural-sort is explicitly requested). One row per worktree:

```
| id | branch | ahead | behind | dirty | stash | DONE? | audit | last-commit |
|----|--------|-------|--------|-------|-------|-------|-------|-------------|
| I1 | parcel/I1-merge | 4 | 0 | 0 | 0 | ✓ | v2 | ab7502d fix(...) |
| I2 | parcel/I2-wire  | 1 | 0 | 3 | 0 | ✗ | —  | 72a2e29 feat(...) |
```

- `DONE?`: `✓` if PARCEL_DONE.md exists, `✗` if not
- `audit`: highest version present (e.g. `v1`, `v2`), or `—` if none
- `last-commit`: short hash + subject, truncated to fit

Below the table, emit a one-line fleet summary:

```
FLEET: <total> parcels, <done-count> DONE, <audited-count> audited, <at-risk> at-risk (dirty or no-DONE-after-N-commits)
```

### Step 5 — Flag at-risk and absorbed parcels

A parcel is **at-risk** if any of:

- `dirty > 0` (uncommitted work that could be lost)
- `ahead > 0` and no `PARCEL_DONE.md` and no commit in the last 2 hours (stalled mid-build)

A parcel is **absorbed** if:

- `ahead == 0` AND `behind >> 0` (e.g. behind ≥ 50) AND PARCEL_DONE is present

Absorbed parcels have shipped their work through main via rebase or squash; the branch is now a stale historical pointer. In practice this is the most common end state for a merged wave — operators should know the parcel is "done, not lost" so they can cleanly garbage-collect worktrees.

A parcel is **fresh** if `ahead > 0` (still has unmerged commits) OR `behind` is small (close to main).

List at-risk rows separately below the main table. Mark absorbed rows with a distinct marker (e.g. trailing `(absorbed)`) so they do not confuse with in-flight parcels. Do not attempt to fix or prune — branch / worktree deletion is a separate concern (`rookery worktree`).

## Hard rules

| Rule | Why |
|---|---|
| Read-only; never `git -C <wt> <mutating>` | Roster must not perturb parcel state |
| Use `git -C <path>`, never `cd` | Compound `cd && ...` breaks permission grants and violates project bash convention |
| Glob for file presence, not Bash `ls`/`find` | Grep/Glob are the dedicated tools; Bash `ls` is context-waste |
| Audits dir is `<worktrees_root>/audits/`, NOT inside the worktree | This is the project's convention — do not look inside the worktree for the audit |
| Never touch `.claude/worktrees/` | Those are lane worktrees; `lane-roster` owns them |
| Resolve `<worktrees_root>` from config, never hardcode | Different deployments use different roots |

## Anti-patterns

| Anti-pattern | Why it hurts |
|---|---|
| `ls <worktrees_root>/` instead of Glob | Bash `ls` adds noise and is slower than Glob for this tree |
| Aborting the whole roster when one worktree's `git log` fails | Single bad worktree should degrade gracefully to a `error: <msg>` row, not kill the report |
| Sorting natural-numeric without the user asking | Alpha sort is predictable; natural sort surprises the caller |
| Embedding audit verdict guesses in the table | Verdict is `parcel-audit`'s remit; this skill reports presence only |
| Re-running `git fetch` to update `main` before counting | Roster is a snapshot of local state; network calls are out of scope |

## See also

- `lane-roster` — the analogue for lane worktrees under `.claude/worktrees/`
- `parcel-reader` — parses a single PARCEL_DONE.md into structured sections
- `parcel-audit` — produces a verdict for one parcel
- `wave-triage` — consumes this roster + audits to compute merge order
- `rookery worktree list` / `rookery status` — CLI views of the same fleet
