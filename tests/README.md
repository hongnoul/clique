# Test plan

Frameworks: pytest + pytest-asyncio. Fakes over mocks where possible:
in-memory sqlite DBs, a FakeModelRuntime that emits canned token
streams with configurable latency, and headless FastAPI servers on
ephemeral ports.

## Unit (`test_router`, `test_context_store`, `test_think`, `test_tools`)
- `common/protocol`: encode/decode round-trip, signature rejection, version mismatch.
- `common/config`: defaults, invalid values, keypair persistence.
- `common/think`: reasoning-effort mapping and prompt notes.
- `scheduler/router.score`: pure-function table tests. Longer prompt prefers bigger model; busy/battery nodes deprioritized; session affinity dominates.
- `scheduler/registry`: first client gets OP, rejoin keeps identity and op level, model change moves clusters.
- `scheduler/context_store`: optimistic version conflict, truncation fits window.
- `scheduler/vcs`: snapshot/diff/rollback round-trip, rollback commits forward.
- `node/tools`: fenced JSON tool calls, step budget, `<done>` terminator.

## Integration (`test_integration`, `test_extended`, `test_tool_loop`)
- Join flow: discover -> register -> warm -> READY visible in registry.
- Task lifecycle: submit -> assign -> stream -> commit; duplicate result rejected; lease expiry requeues with new attempt id.
- One-task-per-node invariant under concurrent submits (race the router).
- Node loss mid-task: heartbeat timeout -> requeue -> second node completes -> exactly one committed result.
- Session migration: pinned node drains, context syncs, versions monotonic.
- Tool loop: agent `read | edit | bash` steps run server-side via `tool_executor`, still reverified before commit.

## Code edits (`test_harness`, `test_code_edit`)
- Fence extraction, path guards, patch budget cap.
- Accepted + rejected patches, parallel-race first-wins, session
  follow-ups, op rollback adds a commit without rewrite.

## Workspaces (`test_live_workspace`)
- Create + write + watch from two clients, deltas cross; stale writers
  rebase; debounced git checkpoints; force flush on task result.

## Headless + MCP (`test_headless`, `test_mcp_server`, `test_mcp_install`)
- CLI smoke (`join/status/submit/nodes/stats/ledger/workspace`) against a
  live headless server; `/repo.bundle` round-trips through `git clone`.
- MCP tools include inference + workspaces and install into harnesses.

## End-to-end (multi-process on localhost, real llama-server optional)
- 3 agents + 1 server on loopback with a tiny GGUF: submit 10 mixed-length tasks, assert routing distribution and no duplicate commits.

## Non-goals for now
- Multi-server/split-brain election, inter-cluster context sharing.
