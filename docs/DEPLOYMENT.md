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

# claude CLI install (project specific; bring your own)
# RUN curl -fsSL https://claude.ai/install.sh | sh

WORKDIR /srv/project
COPY . /srv/project

RUN pip install --no-cache-dir rookery

ENV ROOKERY_CONFIG=/srv/project/rookery.yaml

STOPSIGNAL SIGTERM
CMD ["rookery-daemon"]
```

The container needs persistent storage for `rookery.db`, `worktrees/`, and the OAuth credential store. Mount them as volumes.

## Operational notes

- **Graceful shutdown**: SIGTERM finishes the current tick, signals workers, waits up to `shutdown_grace_s` (default 30s), then flips in-flight jobs back to `pending` so the next start picks them up.
- **Lease reclaim**: stale leases auto-recover on the next tick; `rookery reclaim` forces a sweep.
- **Health**: `rookery daemon-status` tails the pidfile + db heartbeat.
- **Failure modes + recovery**: see the table at the bottom of [BUILD_PLAN](https://github.com/0xDarkMatter/rookery/blob/main/docs/BUILD_PLAN.md) (Windows worktree quirks, lease expiry, locked-file teardown, etc.).
