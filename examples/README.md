# Examples

Two minimal, runnable parcels for smoke-testing a rookery install. Each parcel does the smallest possible work — write a `PARCEL_DONE-<id>.md` file with `Verdict: PASS` — so the focus is the queue mechanics, not the agent task.

| Path | Demonstrates |
|---|---|
| `01-hello-world/parcel.md` | A single parcel, no deps. The simplest end-to-end run. |
| `02-with-deps/parcel-a.md`, `parcel-b.md` | Dependency resolution: `parcel-b` blocks on `parcel-a` reaching `done`. |

## Running an example

From a project with `rookery init` already done, copy the parcel(s) into `parcels/` and enqueue:

```bash
# Hello world
cp examples/01-hello-world/parcel.md parcels/hello-world.md
rookery enqueue hello-world
rookery-daemon

# With deps
cp examples/02-with-deps/parcel-a.md parcels/parcel-a.md
cp examples/02-with-deps/parcel-b.md parcels/parcel-b.md
rookery enqueue parcel-a
rookery enqueue parcel-b --deps parcel-a
rookery-daemon
```

Watch progress with `rookery summary` or `rookery list` in another terminal.
