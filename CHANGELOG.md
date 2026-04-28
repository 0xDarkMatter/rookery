# Changelog

All notable changes to rookery (formerly `claude-fleet`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-04-28

A focused release on **how workers report verdicts to the queue**. Replaces
the v0.2 marker-file IPC (worker writes `PARCEL_DONE-<id>.md`, daemon polls
the filesystem, parses markdown with regex) with a DB-direct protocol where
workers invoke a CLI helper that writes structured rows to the queue's
SQLite db. The marker-file path stays as a legacy fallback.

### Added

- **`rookery parcel done`** — worker reports terminal verdict to the DB.
  Reads `ROOKERY_DB` / `ROOKERY_PARCEL_ID` / `ROOKERY_PARCEL_ATTEMPT` env
  vars (injected by the daemon at worker spawn). Accepts structured
  metadata flags: `--tokens-in`, `--tokens-out`, `--duration-s`,
  `--tests-passed`, `--tests-failed`, `--files-changed`. Optional
  `--detail-file <path>` reads markdown for longer narrative.
  `--write-marker-file` opt-in dual-writes the legacy file for audit.
- **`rookery parcel progress <label>`** — streaming progress events.
  Each call appends a row to `parcel_events` (informational only — daemon
  never gates on these; foundation for the future `rookery watch` TUI).
- **`rookery logs <id> [-f]`** — tail the parcel worker's stdout log.
  `--lines N` for the last N lines, `--follow` / `-f` for tail-follow,
  `--events` interleaves DB progress events.
- **`rookery diff <id>`** — show `git diff <base>...HEAD` inside the
  parcel worktree. Auto-detects the parent repo's default branch
  (`origin/HEAD` → `main` → `master`). Supports `--against <ref>`,
  `--stat`, `--name-only`. Pipes through `delta` if it's on PATH.
- **New `parcel_results` table** — structured verdict storage with
  CHECK constraint on the verdict enum, UNIQUE(job_id, attempt) for
  retry history, and optional metadata columns.
- **New `parcel_events` table** — streaming progress events keyed by
  (job_id, attempt) with `event_type`, `label`, `detail`, optional JSON
  payload.
- **`DbResultAdapter` + `ChainedAdapter`** — new verdict adapters.
  Default config flips from `verdict_adapter: marker-file` to
  `verdict_adapter: chain` (DB-direct first, marker-file fallback).
- **`Orchestrator.write_parcel_result()`, `read_parcel_result()`,
  `append_parcel_event()`, `read_parcel_events()`** — public API for
  the new tables.

### Changed (BREAKING for backend authors)

- **`OrchestratorBackend.harvest()` ABC** now takes a `VerdictAdapter`
  argument and returns a typed `VerdictResult | None` instead of
  `dict[str, object] | None`. Custom backends must update their
  signatures. The daemon owns adapter selection (per-job override via
  `jobs.verdict_adapter`); the backend just runs the adapter.
- **`VerdictResult`** extended with optional structured fields
  (`detail_md`, `tokens_in`, `tokens_out`, `duration_s`, `tests_passed`,
  `tests_failed`, `files_changed`). Existing fields unchanged.
- **`rookery parcel new` template** updated to invoke the new helper
  in the Verdict section instead of writing the marker file by hand.
- **Default `verdict_adapter` in `rookery.yaml`** is now `chain`.
- **`WorkerBackend` constructor** takes optional `db_path: Path`;
  when provided, the four `ROOKERY_*` env vars are injected at spawn.

### Backward compatibility

- Existing parcels writing `PARCEL_DONE-<id>.md` still work — the
  `chain` adapter falls through to `MarkerFileAdapter`. No flag day
  required for v0.2 parcel files.
- Per-job `verdict_adapter` override (`marker-file` / `db` / `chain`)
  honoured exactly as before.
- Existing v0.2 DBs auto-upgrade on first daemon start (migration
  `0006_parcel_results.sql` is idempotent and additive).

## [0.2.0] - 2026-04-27

### Changed (BREAKING)

- **Project renamed `claude-fleet` → `rookery`.** The GitHub repo, PyPI
  distribution, Python package (`import rookery`), CLI binaries, config
  files, and env vars all use the new name.
  - PyPI: `pip install rookery` (was `claude-fleet`)
  - Binaries: `rookery` and `rookery-daemon` (were `claude-fleet` and `claude-fleetd`)
  - Python package: `from rookery...` (was `from claude_fleet...`)
  - Config files: `rookery.yaml`, `rookery.db`, `rookery.pid` (were `claude-fleet.*`)
  - Env vars: `ROOKERY_CONFIG`, `ROOKERY_DB`, `ROOKERY_PROFILES`, `ROOKERY_PIDFILE`
    (were `CLAUDE_FLEET_*`). Hard switch — no fallback.
- The GitHub repo URL is now `github.com/0xDarkMatter/rookery` (auto-redirects from the old name).

### Added

- **Auto-commit on PASS verdict.** When a parcel returns `PASS` /
  `PASS_WITH_WARNINGS`, the daemon now stages and commits any unstaged work in
  the parcel worktree before transitioning the job to `done`. Ensures the
  parcel branch HEAD advances so `auto_land` (and manual `git merge`) have
  something to fast-forward. Opt out via `auto_commit_on_pass: false` in
  `rookery.yaml`.
- **Relative `worktrees_root` resolution.** Paths like `worktrees_root: ./worktrees`
  in `rookery.yaml` are now anchored to the config file's directory at load
  time, so daemons started from a different CWD by pm2 / systemd still resolve
  correctly.
- **`--version` / `-V` flag** on the `rookery` CLI. Reads from
  `importlib.metadata` so it stays in sync with the installed package version.
- **`AGENTS.md`** — guide for AI assistants editing the rookery codebase
  (run commands, conventions, Windows test-flake notes, boundaries).

### Fixed

- `__version__` in `src/rookery/__init__.py` was stuck at `0.1.0`; now
  derives dynamically from package metadata.
- `rookery-daemon start ...` syntax in three docstrings was a lie — Typer
  collapses the single-command app into flat options. Docstrings now match
  reality (`rookery-daemon [--config <path>]`).
- `ruff check src/ tests/` is now clean (was 131 violations; now 0). Pyproject
  ruff config widened line-length to 120 and added ignores for chronic
  intentional patterns (lazy imports, Typer-Option-as-default,
  exception-suffix conventions, complexity warnings on state-machine code).

### Migration

Existing users (none yet — pre-PyPI):
1. `pip uninstall claude-fleet && pip install rookery`
2. Rename `claude-fleet.yaml` → `rookery.yaml`, `claude-fleet.db` → `rookery.db`
3. Replace `claude-fleet`/`claude-fleetd` invocations with `rookery`/`rookery-daemon`
4. Update env var names (`CLAUDE_FLEET_*` → `ROOKERY_*`)
5. Update Python imports (`from claude_fleet...` → `from rookery...`)

[0.2.0]: https://github.com/0xDarkMatter/rookery/releases/tag/v0.2.0

## [0.1.0] - 2026-04-25

Initial extraction from the Axiom benchmarking application as a standalone, reusable runtime.

### Added

- **Orchestrator core** — async daemon loop, tick scheduler, SQLite-backed job queue with WAL, dependency resolution, lease + retry mechanics, structured journal events, and graceful SIGTERM shutdown.
- **Top-level CLI** (`claude-fleet`) — `enqueue`, `list`, `status`, `requeue`, `cancel`, `summary`, `reclaim`, `land`, `land-history`, `land retry`, `init`, `doctor`, `daemon-stop`, `daemon-status`, `parcel new|validate|build`, `worktree list|retire|sweep`.
- **Daemon entry point** (`claude-fleetd`) — foreground runner suitable for pm2 / systemd / docker.
- **Worktree lifecycle** — pluggable `WorktreeLifecycle` ABC with a `GitWorktreeLifecycle` default; auto-create on dispatch, auto-retire on landed, and a sweep command for stale worktrees.
- **Parcel scaffolding** — markdown + YAML frontmatter format, `parcel new` scaffold, `parcel validate` schema check.
- **Verdict adapter framework** — pluggable `VerdictAdapter` ABC with built-in `MarkerFileAdapter`, `ExitCodeAdapter`, and `JsonResultAdapter`; registry lookup via `get_verdict_adapter`.
- **Land pipeline** — manual `land` command, `land-history` audit trail, `land retry` for transient failures, optional auto-land on PASS verdicts.
- **Doctor command** — config + tooling preflight (git, claude, profiles, OAuth, db schema, worktree dir).
- **Init command** — scaffolds `claude-fleet.yaml`, `claude-fleet.db` (with migrations), `parcels/`, and `worktrees/.gitignore` in any git repo.
- **claude-lb integration shim** — optional `[lb]` extra hook for the load-balancer (post-publish wiring).
- **Configuration system** — YAML config (`claude-fleet.yaml`) with env-var overrides (`CLAUDE_FLEET_CONFIG`, `CLAUDE_FLEET_DB`, `CLAUDE_FLEET_PROFILES`).
- **Notifications** — pigeon and webhook hooks (interfaces in place; concrete impls deferred).
- **Documentation** — `README.md`, `docs/QUICKSTART.md`, `docs/DEPLOYMENT.md`, and runnable examples under `examples/`.
- **Test suite** — 378 passing unit tests, 29 documented skips, plus integration scaffolding under `tests/integration/`.

### Notes

- **License**: MIT. See `LICENSE`.
- **`ANTHROPIC_API_KEY` ban**: the daemon refuses to start when `ANTHROPIC_API_KEY` is set in the environment. claude-fleet is OAuth-only — running with a billed API key would silently spend money on every spawned worker. Unset the variable before starting `claude-fleetd`.
- **Known issue (Windows test ordering)**: a single asyncio subprocess test in `tests/unit/orchestrator/test_orchestrator.py` can emit a `RuntimeError: Event loop is closed` warning when test ordering is non-deterministic. The assertion still passes; the warning is a Windows ProactorEventLoop teardown artifact and does not affect runtime behaviour.

[0.1.0]: https://github.com/0xDarkMatter/claude-fleet/releases/tag/v0.1.0
