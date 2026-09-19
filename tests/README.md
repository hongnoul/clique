# Test plan (spec)

Frameworks: pytest + pytest-asyncio. Fakes over mocks where possible: an
in-memory Database, a FakeModelRuntime that emits canned token streams
with configurable latency, and a virtual clock for lease/heartbeat time.

## Unit
- `common/protocol`: encode/decode round-trip, signature rejection, version mismatch.
- `common/config`: defaults, invalid values, keypair persistence.
- `scheduler/router.score`: pure-function table tests. Longer prompt prefers bigger model; busy/battery nodes deprioritized; session affinity dominates.
- `scheduler/registry`: first client gets OP, rejoin keeps identity and op level, model change moves clusters.
- `scheduler/permissions`: op/deop matrix per policy mode, audit entries.
- `scheduler/cron`: expression validation, pending jobs never fire, replayed fire is idempotent.
- `scheduler/context_store`: optimistic version conflict, truncation fits window.
- `scheduler/vcs`: snapshot/diff/rollback round-trip, rollback commits forward.

## Integration (single process, fake runtime)
- Join flow: discover -> register -> warm -> READY visible in registry.
- Task lifecycle: submit -> assign -> stream -> commit; duplicate result rejected; lease expiry requeues with new attempt id.
- One-task-per-node invariant under concurrent submits (race the router).
- Node loss mid-task: heartbeat timeout -> requeue -> second node completes -> exactly one committed result.
- Session migration: pinned node drains, context syncs, versions monotonic.
- Owner reclaim: pause vs release_resources; memory actually reclaimed (assert runtime unload called and health false).

## End-to-end (multi-process on localhost, real llama-server optional)
- 3 agents + 1 server on loopback with a tiny GGUF: submit 10 mixed-length tasks, assert routing distribution and no duplicate commits.
- CLI smoke: `clique join/status/submit/nodes` against the live server.

## Non-goals for now
- Multi-server/split-brain election, inter-cluster context sharing, payment/ledger tests (spec'd elsewhere, not in this scaffold).
