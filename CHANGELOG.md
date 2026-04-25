# Changelog

All notable changes to claude-fleet are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/macknevill/claude-fleet/releases/tag/v0.1.0
