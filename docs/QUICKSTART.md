# Quickstart

A five-minute walkthrough from empty directory to a daemon running parcels.

Prereqs:

- `claude` CLI on `PATH` and authenticated (`claude --version` works)
- `git`, Python 3.11+, `uv`
- `CLAUDE_CODE_OAUTH_TOKEN` set; `ANTHROPIC_API_KEY` **unset** (the default backend refuses to start otherwise)

## 1. Install

```bash
git clone <rookery-repo> rookery
cd rookery
uv pip install -e .
```

Verify:

```bash
rookery --help
# Usage: rookery [OPTIONS] COMMAND [ARGS]...
# ...
```

## 2. Initialise a project

`rookery init` is run inside the project you want managed (it can be the rookery repo itself for a smoke test, or any other git repo).

```bash
mkdir myproject && cd myproject
git init -b main
rookery init
```

Expected output:

```
Created rookery.yaml
Created rookery.db (schema v<N>)
Created parcels/
Created worktrees/.gitignore
Updated .gitignore
```

Resulting layout:

```
myproject/
  rookery.yaml      # config (db_path, max_workers, verdict adapter, ...)
  rookery.db        # SQLite state, WAL mode
  parcels/               # parcel markdown files live here by default
  worktrees/
    .gitignore           # ignores everything in this dir
  .gitignore             # extended with rookery entries
```

## 3. Scaffold a parcel

```bash
rookery parcel new hello-world
# Wrote parcels/hello-world.md
```

Open `parcels/hello-world.md` and edit the body to describe the work. The frontmatter is pre-filled with sensible defaults:

```yaml
---
id: hello-world
priority: 0
deps: []
max_attempts: 3
verification_enabled: true
verdict_adapter: marker-file
auto_land: false
---
```

For the simplest possible parcel, copy `examples/01-hello-world/parcel.md` over the scaffold.

## 4. Validate

```bash
rookery parcel validate parcels/hello-world.md
# OK: parcels/hello-world.md
```

Use `--json` for scriptable output. Validation checks: frontmatter parses, `id` matches the file id, required fields present, `deps` is a list of strings.

## 5. Enqueue

```bash
rookery enqueue hello-world
# Enqueued hello-world (priority=0, deps=[])
```

Inspect:

```bash
rookery list
# id           status   priority  deps  attempts
# hello-world  pending  0         []    0/3

rookery status hello-world
# {"id": "hello-world", "status": "pending", ...}

rookery summary
# pending  1
# total    1
```

## 6. Start the daemon

`rookery-daemon` runs the orchestrator tick loop in the foreground until SIGINT/SIGTERM. For a smoke test, run it in a second terminal:

```bash
rookery-daemon
# [info] daemon starting; max_workers=4, tick_interval_s=5
# [info] tick: claimed hello-world -> running
# [info] worker spawned for hello-world (pid=<n>) in worktrees/hello-world
# ...
```

For background operation, put it under pm2 / systemd / docker. There is no built-in `daemon start` — the daemon is the process; supervise it with the tool of your choice.

In another terminal, watch progress:

```bash
rookery summary
# running 1 / done 0 / failed 0
rookery daemon status
# alive (pid <n>)
```

When the parcel finishes:

```bash
rookery status hello-world
# {"id": "hello-world", "status": "done", "verdict": "PASS", ...}
```

## 7. Stop the daemon

```bash
rookery daemon stop
# Sent SIGTERM to pid <n>
```

The daemon terminates running workers cleanly and flips their jobs back to `pending` for the next start.

## Where things live

| Path | What |
|---|---|
| `rookery.yaml` | Config (edit to taste) |
| `rookery.db` | SQLite state — `jobs`, `land_events`, etc. |
| `rookery.pid` | Daemon pidfile (override with `--pidfile`) |
| `parcels/<id>.md` | Parcel prompt + frontmatter (one per job) |
| `worktrees/<id>/` | Per-job git worktree, created on claim |
| `worktrees/<id>/parcel.log` | Raw stdout/stderr from the worker's `claude -p` invocation |
| `worktrees/<id>/PARCEL_DONE-<id>.md` | Verdict marker the agent writes |
| `logs/` | Daemon stdout (JSON lines), land-events log |

## Notes for this build

- `rookery doctor` is currently a stub printing TODO. Real preflight checks land in P6 (G7) — for now, manually verify the prereqs at the top of this doc.
- Auto-land defaults to `false`. To exercise it, set `auto_land: true` in `rookery.yaml` and provide `auto_land_test_cmd`.
- `rookery-daemon --profiles a,b,c` rotates OAuth profiles round-robin across workers. Point claude-lb at this once it lands.

## Next

- See `examples/02-with-deps/` for a two-parcel dependency example
- See the README's Queue operations section for `cancel`, `requeue`, `reclaim`, and worktree management
- Read `rookery.yaml` — every field has a comment
