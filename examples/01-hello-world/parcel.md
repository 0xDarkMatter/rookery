---
id: hello-world
priority: 0
deps: []
max_attempts: 3
verification_enabled: true
verdict_adapter: marker-file
auto_land: false
notes: |
  Smallest possible parcel. Writes a PASS verdict marker and exits.
---

# Hello, rookery

You are running inside a fresh git worktree under a `rookery` daemon. The body of this file is your prompt.

The job for this parcel is trivial: confirm the verdict pipeline works.

## Acceptance

- Write a single file at the worktree root named `PARCEL_DONE-hello-world.md`.
- The file must contain a line `Verdict: PASS`.
- The summary should be the one-line string `Hello, rookery`.
- Do not modify any other files. Do not create any commits.

## Verdict file shape

Write exactly this content to `PARCEL_DONE-hello-world.md`:

```markdown
# PARCEL_DONE: hello-world

Verdict: PASS

## Summary
Hello, rookery
```

When that file exists, the daemon's verdict adapter will read it, mark this parcel `done`, and retire the worker. No further action is needed from you.
