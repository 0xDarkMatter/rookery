![rookery](docs/assets/rookery-banner.png)

**Built for the Claude Opus 4.7 Hackathon.**

[![Hackathon](https://img.shields.io/badge/Claude%20Opus%204.7-Hackathon-blueviolet?logo=anthropic)](https://www.anthropic.com/)

_Formerly known as `claude-fleet` (renamed 2026-04-27). As of v0.2.0 the Python package, PyPI distribution, and CLI binaries (`rookery` and `rookery-daemon`) all ship under the new name; env vars are `ROOKERY_*`, config files are `rookery.yaml` / `rookery.db`._

**A job system for headless agent sessions.** Run dozens of unattended `claude -p` workers in parallel, each in its own git worktree, with dependency resolution, lease-based crash recovery, and optional auto-land on PASS verdicts. One worker per parcel, one parcel per worktree, one daemon per project.

> _We dogfooded the primitive to build the primitive._ This repo was extracted from `axiom`'s orchestrator by a single headless `claude -p` session — itself a parallel parcel run coordinated by the very state machine it was building. The commit history is the proof; jump to [Receipts](#receipts).

![rookery](docs/assets/hackathon.gif)

```
$ rookery enqueue migrate-auth --deps schema
$ rookery enqueue add-oauth-flow --deps migrate-auth
$ rookery-daemon                              # daemon picks them up

# Six hours later:
$ rookery summary
status      count
─────────  ──────
landed       2
done         0
running      0
pending      0
```

That's the whole shape. Throw markdown parcels at it, walk away, come back to merged commits.

## Recent Updates

**v0.2.0** (April 2026) — **BREAKING**
- 🪶 **Renamed `claude-fleet` → `rookery`.** PyPI package, Python imports (`from rookery...`), CLI binaries (`rookery`, `rookery-daemon`), config files (`rookery.yaml`, `rookery.db`, `rookery.pid`), and env vars (`ROOKERY_*`) all use the new name. Hard switch — no fallback. See [CHANGELOG](CHANGELOG.md) for migration steps.
- ✨ **Auto-commit on PASS verdict.** Daemon now stages and commits any unstaged work in the parcel worktree after a `PASS` / `PASS_WITH_WARNINGS` verdict so `auto_land` (and manual merge) have a HEAD to fast-forward. Opt out via `auto_commit_on_pass: false`.
- 🔢 **`rookery --version` / `-V`** flag (reads from `importlib.metadata`).
- 📐 **Relative `worktrees_root` resolution** anchored to the config file's directory — pm2 / systemd setups starting the daemon from a different CWD now resolve worktree paths correctly.
- 🤖 **[AGENTS.md](AGENTS.md)** — guide for AI assistants editing the rookery codebase.
- 🧹 **Lint hygiene**: `ruff check src/ tests/` now clean; sane ignores for the chronic intentional patterns.

**v0.1.0** (April 2026)
- 🚀 **Initial extraction** - Lifted ~7,500 LOC from `axiom`'s orchestrator by a single headless `claude -p` session running unattended against six pages of spec docs. The build itself was a DSP wave — phases P0–P8 as sub-parcels coordinated by the very state machine the codebase now exposes as a library. 204 tests green on first run. MIT, Python 3.12+, cross-platform (Linux, macOS, Windows).

[View full changelog →](https://github.com/0xDarkMatter/rookery/commits/main)

## Why this exists

Every other parallel-Claude tool today is one of three shapes:

| Tool | Shape | What it doesn't do |
|---|---|---|
| `claude-squad`, `claude-flow` | UI wrapper | Headless. Unattended. Crash recovery. |
| LangGraph, CrewAI | Workflow library | Worktree isolation. Auto-land. State persistence. |
| `for f in *.md; do claude -p < $f; done` | Shell loop | Dependencies. Retries. Anything if it dies. |

`rookery` is a fourth shape: a **job system** specialised for headless agent sessions, where the worktree is the isolation unit and the daemon survives operator absence. Closest analogue is `make` + `git worktree` + a job queue + a CI runner, fused.

## The trilogy

```
┌─────────────────────────────────────────────────────┐
│ axiom            ← multi-agent benchmarking app      │
│   uses ↓                                             │
├─────────────────────────────────────────────────────┤
│ rookery     ← runtime: queue + daemon + worktrees   ◀── you are here
│   uses ↓ (optional)                                  │
├─────────────────────────────────────────────────────┤
│ claude-lb        ← OAuth profile rotation            │
└─────────────────────────────────────────────────────┘
```

Each layer is independently useful. Use rookery alone for single-account local builds. Layer claude-lb under it when you need to fan out across multiple Max plans. Build something axiom-shaped on top when you need agent topology + skill libraries.

## Quickstart (greenfield — 60 seconds)

```bash
mkdir my-fleet && cd my-fleet
git init -b main && git commit --allow-empty -m init

uv pip install rookery
rookery --version      # confirm install (e.g. "rookery 0.2.0")
rookery init           # scaffolds yaml, db, parcels/, worktrees/
rookery doctor         # verifies env, claude binary, OAuth, git

rookery parcel new hello-world
# edit parcels/hello-world.md — describe the task

rookery enqueue hello-world
rookery-daemon               # foreground daemon; Ctrl-C to stop
```

In another terminal:

```bash
rookery status hello-world
# ... watch it move pending → claimed → running → done
```

Five-minute walkthrough: [docs/QUICKSTART.md](docs/QUICKSTART.md).

## Adapting to an existing repo

`rookery` is happiest in greenfield projects, but it works fine alongside an existing codebase. We extracted it *from* `axiom`, a sibling project that uses this same orchestrator pattern internally. The retrofit recipe:

1. **`rookery init`** in the repo root — it only writes new files (`rookery.yaml`, `rookery.db`, `parcels/`, `worktrees/.gitignore`). Adds five lines to `.gitignore`. Touches nothing else.
2. **Pick narrow first targets.** Good first parcels: dependency upgrades, lint cleanups, test scaffolding for a leaf module, codemods. Bad first parcels: anything that touches the build system, anything with cross-cutting refactors, anything where the contract between agents isn't already locked down.
3. **Pin `auto_land: false` initially.** Watch verdicts, eyeball the diffs, merge by hand. Flip to `auto_land: true` per-parcel once you trust the test signal.
4. **Use `--deps` to gate by file scope.** Two parcels editing the same module = chain them. Two parcels editing different modules = run them concurrently. Worktree isolation prevents most stomping but not all.
5. **Keep parcels < 90 minutes of agent time.** Longer = lease expiry risk + harder to debug verdicts. Decompose larger work into a parcel chain.

Specific gotchas at scale:

- Worktree creation on a large repo with submodules can take ~15 seconds — tune `lease_seconds` accordingly (default 1800s is generous).
- If your test suite is slow (>5 min), set `auto_land_test_cmd` to a scoped command, not the full suite. The land flow runs it twice (after rebase, before fast-forward).
- Windows + git worktrees: works, but NTFS junctions occasionally lock on `worktree remove`. We retry with backoff; if you see `worktree.WorktreeLockError`, that's it.

## Parcel format

A parcel is one markdown file with YAML frontmatter. The body is the prompt sent to `claude -p`.

```markdown
---
id: add-oauth-flow         # must match the enqueue id
priority: 5                # higher runs first; default 0
deps: [schema-migration]   # ids of parcels that must finish first
max_attempts: 3            # retry cap before -> blocked
auto_land: false           # opt-in per-parcel; overrides global
verdict_adapter: marker-file   # marker-file | exit-code | json-result
---

# Add OAuth flow

You are working in a fresh git worktree. Implement OAuth2 in `src/auth/oauth.py`.

## Acceptance
- All tests in `tests/auth/` pass
- `ruff check src/auth/` clean

## Verdict
When you finish, write `PARCEL_DONE-add-oauth-flow.md` at the worktree root:

    Verdict: PASS

    ## Summary
    <one paragraph>
```

The default verdict adapter (`marker-file`) reads the first `Verdict:` line. Values: `PASS`, `PASS_WITH_WARNINGS`, `BLOCK`, `UNKNOWN`. Other built-ins: `exit-code`, `json-result`. Or implement `VerdictAdapter` for your own.

## Daemon control

```bash
rookery-daemon                                # foreground; canonical pm2/systemd target
rookery-daemon --profiles max-1,max-2,max-3   # round-robin OAuth profiles

rookery daemon-status                   # liveness via pidfile
rookery daemon-stop                     # SIGTERM
```

Daemon writes its pid to `rookery.pid`. On shutdown it terminates child workers and flips their jobs back to `pending` so the next start picks them up. Crash mid-job? Lease expires after 30 min, job returns to `pending`, retry counter increments. Three failed attempts → `blocked` (operator-only recovery via `requeue`).

## Queue operations

```bash
rookery enqueue <id> [--deps a,b] [--priority N] [--no-verify]
rookery list [--status pending|running|done|failed|blocked|all]
rookery status <id> [--json]
rookery summary [--json]

rookery cancel <id>           # -> failed (terminal)
rookery requeue <id>          # blocked/failed -> pending, attempts reset
rookery reclaim               # one-shot expired-lease sweep

rookery land <id>             # manual land of a PASS'd job
rookery land retry <id>       # retry a merge-blocked land
rookery land history <id>     # land_events rows for a job

rookery worktree list
rookery worktree retire <id>
rookery worktree sweep [--dry-run]
```

## State machine

```
   enqueue ─► pending ──► claimed ──► running ──► done/audited
                  ▲          │            │            │
                  │          │            │       (verdict=PASS
                  │          │            │        + auto_land)
                  │       lease           │            ▼
                  └─── expired ◄──────────┴───      landing
                                                      │
                                              ┌───────┴────────┐
                                              ▼                ▼
                                            landed       merge-blocked
                                          (commit_sha)   (operator
                                                          retry only)
```

Every transition writes a row to SQLite (`jobs` table) and emits a JSON line to the daemon log. The whole machine survives daemon restart — `BEGIN IMMEDIATE` plus WAL mode means two daemons can't claim the same job and a crash never loses state.

## Auto-commit on PASS

When a worker emits a `PASS` or `PASS_WITH_WARNINGS` verdict, the daemon stages and commits any unstaged work in the parcel worktree before transitioning the job to `done`. This guarantees the parcel branch HEAD advances so `auto_land` (and manual `git merge`) have something to fast-forward — without it, a worker that finishes its task but forgets to commit leaves the branch empty and the land flow has nothing to merge.

The commit subject is derived from the parcel-done `## Summary` section (truncated to 72 chars) with the parcel id as the conventional-commit scope:

```
feat(<parcel-id>): <first non-empty line under ## Summary>
```

Disable per-deployment with `auto_commit_on_pass: false` in `rookery.yaml` if you'd rather have the worker be solely responsible for committing.

Skipped automatically when the worker already committed (`git diff --cached --quiet` returns 0). Failures are logged as `orchestrator.daemon.auto_commit_failed` and recorded on the job's `last_error`, but **do not block** the verdict transition — a successful parcel still goes to `done` even if auto-commit can't run.

## Configuration paths

`rookery.yaml` keys are read in this resolution order:
1. Explicit CLI flag (e.g. `--config <path>`, `--db <path>`)
2. Env var (`ROOKERY_CONFIG`, `ROOKERY_DB`, `ROOKERY_PROFILES`, `ROOKERY_PIDFILE`)
3. YAML file value (with `worktree_base` → `worktrees_root` alias preserved for migration)
4. Hard-coded defaults (`./rookery.yaml`, `./rookery.db`, `./rookery.pid`, `./worktrees`)

`worktrees_root: ./worktrees` in `rookery.yaml` is anchored to the **config file's directory**, not the daemon's CWD — important when pm2 / systemd starts the daemon from a different working directory.

## Pluggable

| Interface | Default | Custom |
|---|---|---|
| `OrchestratorBackend` | `WorkerBackend` (subprocess `claude -p`) | Implement for Anthropic Managed Agents, container-based isolation, etc. |
| `VerdictAdapter` | `MarkerFileAdapter` (reads `PARCEL_DONE-<id>.md`) | `ExitCodeAdapter`, `JsonResultAdapter`, or your own |
| `WorktreeLifecycle` | `GitWorktreeLifecycle` | Override for non-git or container-backed isolation |
| `Notifier` | `LogNotifier` | Hook to pigeon, slack, webhooks for `parcel_landed`/`merge_blocked`/`lease_expired` |
| Profile selector | env-var (`ROOKERY_PROFILES`) round-robin | `[lb]` extra delegates to `claude-lb` |

Each interface is a small ABC with a single concrete default. The wiring point is constructor injection in `src/rookery/orchestrator/__main__.py` — see [AGENTS.md](AGENTS.md) for adding-a-feature recipes.

## Receipts

### We dogfooded the primitive to build the primitive

This repo was extracted from `axiom`'s orchestrator (~6,749 LOC of in-production code) by a single headless `claude -p` session running against six pages of spec docs. **The build itself was a DSP wave** — each phase (P0–P8) a sub-parcel, each gap (G1–G10) a sub-sub-parcel, all coordinated by the same kind of state machine and worktree-isolation pattern that the runtime now exposes as a library.

The git history is the proof. Read it bottom-to-top:

```
feat(P8): polish, CHANGELOG, v0.1.0 tag
feat(P7/G9): land retry command
feat(P7/G8): worktree sweep command
feat(P7/G5): claude-lb integration shim
feat(P6/G2): worktree auto-retire on landed
feat(P6/G4): pluggable verdict adapter ABC + registry
feat(P6/G7): doctor command
docs(P5/G10): README + QUICKSTART + examples
feat(P5/G1): worktree lifecycle ABC + GitWorktreeLifecycle
feat(P5/G3): parcel scaffold + validate
feat(P5/G6): init command
feat: P4 — lift tests from Axiom, all green (204 passed, 29 skipped)
feat: P3 -- top-level CLI surface
feat: P2 — decouple from axiom
feat: P1 — lift orchestrator core from axiom
chore: P0 — bootstrap repo
```

That's a real DSP run, with real verdicts, against the very pattern the codebase codifies. No human typed those commits — the agent did, working unattended for under an hour. We watched the queue do its job.

### By the numbers

- **204 unit + integration tests** lifted from axiom, all green on first run
- **~7,500 LOC** in `src/rookery/`
- **Extracted from sibling project `axiom`** — same orchestrator pattern used internally
- **MIT licensed**, Python 3.12+
- **Cross-platform** — Linux, macOS, Windows (including the NTFS worktree quirks)

## Where to read more

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — five-minute walkthrough
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — pm2, systemd, docker examples
- [AGENTS.md](AGENTS.md) — guide for AI assistants editing the rookery codebase
- [examples/](examples/) — runnable parcel examples (`01-hello-world`, `02-with-deps`)
- [CHANGELOG.md](CHANGELOG.md) — release notes (v0.1.0, v0.2.0)

The parcel format and pluggable interfaces are documented inline in this README — the [Parcel format](#parcel-format) and [Pluggable](#pluggable) sections above are the canonical reference. Standalone `docs/PARCEL_FORMAT.md` / `docs/INTEGRATIONS.md` are planned for v0.3.

## License

MIT. See [LICENSE](LICENSE).
