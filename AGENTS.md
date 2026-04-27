# AGENTS.md — guide for AI assistants working in this repo

Guidance for Claude / Codex / Copilot / Aider / etc. when editing the **rookery** codebase. Human contributors: see [README.md](README.md) and [docs/QUICKSTART.md](docs/QUICKSTART.md).

This file is for the *meta-case*: an AI agent editing the orchestrator that runs other AI agents. Worker parcels running inside rookery itself do **not** read this file — they read their own parcel markdown.

## Project shape

```
src/rookery/                     # the package
├── adapters/                    # VerdictAdapter ABC + 3 built-in implementations
├── cli/                         # Typer subcommands (one file per group)
├── orchestrator/                # async daemon, queue, schema, backends
│   ├── __main__.py              # rookery-daemon entrypoint
│   └── migrations/*.sql         # additive-only schema migrations
├── platform/                    # claude-lb shim, headless spawn, worktree IDs
├── doctor.py                    # preflight checks
├── init.py                      # `rookery init` scaffolding
└── worktree.py                  # WorktreeLifecycle ABC + GitWorktreeLifecycle
```

Tests mirror the source layout under `tests/unit/` and `tests/integration/`.

## Run commands

| Task | Command |
|---|---|
| Install editable | `uv pip install -e .` |
| Unit tests | `uv run pytest tests/unit/ -q` |
| Integration tests | `uv run pytest tests/integration/ -q` |
| Lint | `uv run ruff check src/ tests/` (must pass clean before commit) |
| Type check | `uv run mypy src/` (advisory — pre-existing failures exist in test fixtures) |
| Build wheel | `uv build` (produces `dist/rookery-<version>-*.whl`) |
| Live smoke | `rookery init && rookery doctor && rookery parcel new x && rookery enqueue x` (in scratch dir) |

## Conventions

| Topic | Rule |
|---|---|
| Imports | Top-of-module by default. Lazy imports allowed for slow deps (rich, structlog, the orchestrator core inside CLI commands) — mark with `# noqa: PLC0415` |
| Async | The orchestrator + daemon + worker spawn are async. CLI commands stay sync; they `asyncio.run()` to bridge |
| Errors | Raise specific classes (e.g. `WorktreeRetireError`, `InitError`). Don't catch broad `Exception` except in the daemon's verdict-handling boundary |
| Logging | `structlog.get_logger()` in orchestrator/daemon/backend code; `rich.console.Console()` in CLI surfaces |
| SQL | All schema changes go in `src/rookery/orchestrator/migrations/NNNN_*.sql` — additive only, no destructive migrations. Apply with `apply_migrations(db_path)` |
| Tests | `pytest-asyncio` mode is `auto` (no `@pytest.mark.asyncio` needed). Integration tests use `FakeBackend` — never spawn real `claude -p` from a test |
| Commits | Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`). Subject under 72 chars, no trailing period, imperative mood |
| Versions | `pyproject.toml` is the source of truth. `src/rookery/__init__.py` reads `__version__` from `importlib.metadata` |

## Known windows quirks

- **`test_retire.py` flake**: certain test orderings cause `test_gate_uncommitted_changes` / `test_gate_active_process` / `test_all_gates_green` / `test_gate_branch_not_merged` to fail because the `recent_write` mtime gate fires before the gate it should fire after. Pre-existing, documented in BUILD_DONE.md (gitignored). Run `pytest tests/unit/ -p no:randomly` for deterministic ordering, or run the affected tests in isolation.
- **Pytest tmpdir cleanup**: pytest's atexit `cleanup_dead_symlinks` raises `PermissionError` on `C:\Users\...\AppData\Local\Temp\pytest-of-Mack\pytest-current` when a prior session left handles open. Harmless — assertions pass. Don't try to fix with `addopts = "--basetemp=..."` — that introduces *real* failures because integration tests share state with the unit tests' tmpdir.
- **Worktree `git worktree remove`**: NTFS junction locks during teardown — the lifecycle layer retries 3× with 1 s back-off. Don't remove the retry; it's load-bearing.

## Boundaries — don't touch

- `.claude/worktrees/` — agent session state, may be live work. Never `rm -rf`, never `git add -A` if any `.claude/worktrees/` paths show as untracked
- `BUILD_DONE.md` is gitignored — historical receipts from the v0.1.0 build. Don't try to commit it
- The v0.1.0 entry in `CHANGELOG.md` is historical record — don't rewrite, only append new versions above it

## Adding a feature

1. **Schema change?** New migration file, additive only. Test against a fresh DB and a v0.1.0 DB.
2. **New CLI command?** Add to `src/rookery/cli/<group>.py`, register in `src/rookery/cli/__init__.py`. Help text must end with the env var hint if it reads one (`[env var: ROOKERY_*]` is auto-added by Typer).
3. **New verdict adapter?** Implement `VerdictAdapter`, register in `adapters/registry.py`, add a unit test under `tests/unit/test_adapters.py`.
4. **New worktree lifecycle?** Implement `WorktreeLifecycle` (3 methods: `create`, `exists`, `retire`). Wire via constructor injection — don't import the new class anywhere outside the wiring point.
5. **Run the full check**: `ruff check src/ tests/ && pytest tests/unit/ -p no:randomly && pytest tests/integration/`. All three must pass.
6. **CHANGELOG entry** under a new version header, plus a row in README's "Recent Updates" section.

## What NOT to do

- Don't add `claude-fleet` as a backward-compat alias for `rookery`. The v0.2.0 rename was a hard switch by design.
- Don't add ANTHROPIC_API_KEY support. The project is OAuth-only — silent API spend would burn user credit on every worker.
- Don't introduce sync subprocess calls in the orchestrator hot loop. Use `asyncio.to_thread(subprocess.run, ...)` if a sync call is genuinely necessary.
- Don't rewrite `git worktree remove` to use `shutil.rmtree`. The git command knows about the worktree metadata; the bare filesystem call leaves dangling refs.
- Don't push without explicit user permission. Use `git push origin <branch>` only after the user confirms the diff.

## Pull request shape

A good rookery PR:
- One feature or one fix, not both
- Matching test added (unit if possible, integration if it crosses subprocess boundaries)
- CHANGELOG entry
- README "Recent Updates" row if user-visible
- `ruff` + `pytest tests/unit/` clean

The build was done as a DSP wave by a single headless `claude -p` session (see [README.md#receipts](README.md#receipts)) — every commit is self-contained because each was a parcel. New PRs should follow that shape.
