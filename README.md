# claude-fleet

Persistent parcel-dispatch queue and async daemon for parallel headless `claude -p` sessions. SQLite-backed state machine with dependency resolution, lease/retry mechanics, and optional auto-land on PASS verdicts. One worker per parcel, one parcel per git worktree, one daemon per project.

`claude-fleet` is the middle piece of a three-part stack:

- **Axiom** — the multi-agent benchmarking application that originated this runtime
- **claude-fleet** — the extracted runtime: queue + daemon + worktree lifecycle (this repo)
- **claude-lb** — an optional load-balancer that rotates OAuth profiles across Claude Max plans, layered underneath when you want to fan out beyond a single account

You can use claude-fleet by itself. The other two are optional.

## Quickstart

```bash
uv pip install -e .
claude-fleet init
claude-fleet parcel new hello-world
# edit parcels/hello-world.md
claude-fleet enqueue hello-world
claude-fleetd            # foreground daemon; Ctrl-C to stop
```

`claude-fleet init` scaffolds `claude-fleet.yaml`, an empty `claude-fleet.db` (with schema migrations applied), `parcels/`, and `worktrees/.gitignore`. `claude-fleetd` runs the orchestrator tick loop in the foreground and is the canonical thing to put under pm2, systemd, or docker.

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for the five-minute walkthrough.

## Parcel format

A parcel is one markdown file with YAML frontmatter. The body is the prompt sent to `claude -p`.

```markdown
---
id: add-oauth-flow         # must match the enqueue id
priority: 5                # higher runs first; default 0
deps: [schema-migration]   # ids of parcels that must finish first
max_attempts: 3            # retry cap before -> blocked
verification_enabled: true
auto_land: false
---

# Add OAuth flow

You are working in a fresh git worktree. Implement OAuth2 in `src/auth/oauth.py`.

## Verdict

When you finish, write `PARCEL_DONE-add-oauth-flow.md` at the worktree root with:

    Verdict: PASS

    ## Summary
    <one paragraph>
```

The default verdict adapter (`marker-file`) reads the first `Verdict:` line. Values: `PASS`, `PASS_WITH_WARNINGS`, `BLOCK`, `UNKNOWN`. Full reference: [docs/PARCEL_FORMAT.md](docs/PARCEL_FORMAT.md) (placeholder until P5 closes; for now see API.md in the spec repo).

## Daemon control

```bash
claude-fleetd                      # run in foreground
claude-fleetd --strip-auth-env     # force workers to use profile credentials
claude-fleetd --profiles max-1,max-2,max-3   # round-robin across profiles

claude-fleet daemon status         # liveness check via pidfile
claude-fleet daemon stop           # SIGTERM to running daemon
```

The daemon writes its pid to `claude-fleet.pid` by default. On shutdown it terminates child workers and flips their jobs back to `pending` so the next start picks them up.

## Queue operations

```bash
claude-fleet enqueue <id> [--deps a,b] [--priority N] [--no-verify]
claude-fleet list [--status pending|running|done|failed|blocked|all]
claude-fleet status <id>
claude-fleet summary [--json]

claude-fleet cancel <id>           # -> failed (terminal)
claude-fleet requeue <id>          # blocked/failed -> pending, attempts reset
claude-fleet reclaim               # one-shot expired-lease sweep

claude-fleet land retry <id>       # retry a merge-blocked land
claude-fleet land history <id>     # land_events rows for a job

claude-fleet worktree list
claude-fleet worktree retire <id>
claude-fleet worktree sweep [--dry-run]
```

`claude-fleet doctor` is reserved for P6 (G7); today it prints a TODO marker.

## Optional integrations

- **claude-lb** — deferred. When installed and enabled in `claude-fleet.yaml`, the daemon delegates worker auth to claude-lb's profile rotation. Until then, `claude-fleetd --profiles a,b,c` covers single-host round-robin.
- **Custom verdict adapters** — implement `VerdictAdapter` in Python; wire via `verdict_adapter:` in config.
- **Custom backends** — implement `OrchestratorBackend` for non-subprocess execution (e.g. Anthropic Managed Agents).

## Where to read more

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — five-minute walkthrough
- `examples/` — runnable parcel examples (`01-hello-world`, `02-with-deps`)
- Spec docs (in the hackathon project): `PROJECT.md`, `ARCHITECTURE.md`, `API.md`

## License

MIT. See [LICENSE](LICENSE).
