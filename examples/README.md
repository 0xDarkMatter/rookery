# Examples

Two minimal, runnable parcels for smoke-testing a claude-fleet install. Each parcel does the smallest possible work — write a `PARCEL_DONE-<id>.md` file with `Verdict: PASS` — so the focus is the queue mechanics, not the agent task.

| Path | Demonstrates |
|---|---|
| `01-hello-world/parcel.md` | A single parcel, no deps. The simplest end-to-end run. |
| `02-with-deps/parcel-a.md`, `parcel-b.md` | Dependency resolution: `parcel-b` blocks on `parcel-a` reaching `done`. |

## Running an example

From a project with `claude-fleet init` already done, copy the parcel(s) into `parcels/` and enqueue:

```bash
# Hello world
cp examples/01-hello-world/parcel.md parcels/hello-world.md
claude-fleet enqueue hello-world
claude-fleetd

# With deps
cp examples/02-with-deps/parcel-a.md parcels/parcel-a.md
cp examples/02-with-deps/parcel-b.md parcels/parcel-b.md
claude-fleet enqueue parcel-a
claude-fleet enqueue parcel-b --deps parcel-a
claude-fleetd
```

Watch progress with `claude-fleet summary` or `claude-fleet list` in another terminal.
