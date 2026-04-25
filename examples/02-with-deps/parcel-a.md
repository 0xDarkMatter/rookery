---
id: parcel-a
priority: 1
deps: []
max_attempts: 3
verification_enabled: true
verdict_adapter: marker-file
auto_land: false
notes: |
  First half of the dependency demo. Runs unconditionally; parcel-b waits for this to reach `done`.
---

# Parcel A (no dependencies)

You are running inside a fresh git worktree under a `claude-fleet` daemon. This parcel runs first; `parcel-b` is blocked until you reach a terminal `done` status.

## Acceptance

- Write a single file at the worktree root named `PARCEL_DONE-parcel-a.md`.
- The file must contain a line `Verdict: PASS`.
- Do not modify any other files. Do not create any commits.

## Verdict file shape

Write exactly this content:

```markdown
# PARCEL_DONE: parcel-a

Verdict: PASS

## Summary
Parcel A finished. Parcel B may now proceed.
```
