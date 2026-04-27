# Deployment

`rookery-daemon` is a long-running foreground process. Put it under any process supervisor that handles SIGTERM cleanly. Three reference recipes follow.

See [QUICKSTART.md](QUICKSTART.md) for first-run setup. See the README for config reference.

## Pre-flight

Before deploying, in the project directory:

```bash
rookery init           # scaffolds config + db + dirs
rookery doctor         # confirms git, claude, profiles, OAuth, schema
unset ANTHROPIC_API_KEY     # daemon refuses to start with this set
```

The daemon writes `rookery.pid` next to the database. Each supervisor below reuses that pidfile so `rookery daemon-stop` keeps working.

## pm2

`ecosystem.config.cjs`:

```javascript
module.exports = {
  apps: [
    {
      name: "rookery",
      script: "rookery-daemon",
      cwd: "/srv/myproject",
      env: {
        ROOKERY_CONFIG: "/srv/myproject/rookery.yaml",
      },
      autorestart: true,
      max_restarts: 10,
      kill_timeout: 30000,
    },
  ],
};
```

Start: `pm2 start ecosystem.config.cjs && pm2 save`.

## systemd

`/etc/systemd/system/rookery.service`:

```ini
[Unit]
Description=rookery daemon
After=network.target

[Service]
Type=simple
User=fleet
WorkingDirectory=/srv/myproject
Environment=ROOKERY_CONFIG=/srv/myproject/rookery.yaml
ExecStart=/usr/local/bin/rookery-daemon
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

Enable: `systemctl daemon-reload && systemctl enable --now rookery`.

## Docker

`Dockerfile`:

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl \
 && rm -rf /var/lib/apt/lists/*

# Install uv (the modern Python toolchain — 10-100× faster than pip)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# claude CLI install (project specific; bring your own)
# RUN curl -fsSL https://claude.ai/install.sh | sh

WORKDIR /srv/project
COPY . /srv/project

# Install rookery as an isolated CLI tool (lands binaries on PATH)
RUN uv tool install git+https://github.com/0xDarkMatter/rookery.git

ENV ROOKERY_CONFIG=/srv/project/rookery.yaml \
    PATH="/root/.local/bin:${PATH}"

STOPSIGNAL SIGTERM
CMD ["rookery-daemon"]
```

The container needs persistent storage for `rookery.db`, `worktrees/`, and the OAuth credential store. Mount them as volumes.

## Operational notes

- **Graceful shutdown**: SIGTERM finishes the current tick, signals workers, waits up to `shutdown_grace_s` (default 30s), then flips in-flight jobs back to `pending` so the next start picks them up.
- **Lease reclaim**: stale leases auto-recover on the next tick; `rookery reclaim` forces a sweep.
- **Health**: `rookery daemon-status` reads the pidfile + db heartbeat (use this in load-balancer health checks).
- **Auto-commit on PASS**: when a parcel finishes with `PASS`/`PASS_WITH_WARNINGS` and leaves unstaged changes, the daemon stages and commits with a `feat(<parcel-id>): <summary>` message before transitioning the job to `done`. The commit is local to the worktree — `auto_land` (or a manual `git merge`) is what brings it onto `main`. Disable per-deployment with `auto_commit_on_pass: false` in `rookery.yaml` if your workers always commit themselves.
- **Working directory matters**: pm2 / systemd / docker MUST start the daemon with the correct `cwd` (the project directory). Relative paths in `rookery.yaml` (e.g. `worktrees_root: ./worktrees`) are resolved relative to the **config file's directory** at load time, but `db_path` and the pidfile resolve against the daemon's CWD. Use absolute paths in `rookery.yaml` if your supervisor's working directory is unstable.
- **Env vars**: `ROOKERY_CONFIG`, `ROOKERY_DB`, `ROOKERY_PROFILES`, `ROOKERY_PIDFILE` override CLI flags via Typer's `envvar=` wiring — pass them through pm2 `env: {...}`, the systemd `Environment=` directive, or `docker run -e`.
- **Failure modes + recovery**: see the README's State machine section + Auto-commit on PASS for the canonical transitions. Common gotchas:
  - Windows worktree teardown can fail under file-locks — daemon retries up to 3× with 1 s back-off
  - Lease expiry returns claimed jobs to `pending`; the retry counter increments on each lease cycle
  - Three failed attempts → `blocked` (operator-only recovery via `rookery requeue <id>`)
