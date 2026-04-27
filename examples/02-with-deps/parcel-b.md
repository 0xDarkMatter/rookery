---
id: parcel-b
priority: 1
deps:
  - parcel-a
max_attempts: 3
verification_enabled: true
verdict_adapter: marker-file
auto_land: false
notes: |
  Second half of the dependency demo. Will sit in `pending` until parcel-a reaches `done`.
  When the daemon ticks and sees parcel-a's status is terminal, parcel-b becomes claimable.
---

# Parcel B (depends on parcel-a)

You are running inside a fresh git worktree under a `rookery` daemon. By the time you receive this prompt, `parcel-a` has already reached `done` — its dependency was honoured by the queue, not by you.

## Acceptance

- Write a single file at the worktree root named `PARCEL_DONE-parcel-b.md`.
- The file must contain a line `Verdict: PASS`.
- Do not modify any other files. Do not create any commits.

## Verdict file shape

Write exactly this content:

```markdown
# PARCEL_DONE: parcel-b

Verdict: PASS

## Summary
Parcel B finished. Dependency on parcel-a was satisfied.
```
