# Clique: the networked personal AI computer — implementation spec

[Overview](README.md)

Fully implemented. Every `.py` file in this tree contains the working
implementation with docstring contracts; `tests/` runs a real server
plus real agents over HTTP/WS with an echo runtime. Dogfooded on a
live ASUS Ascent GX10 server (docs/dogfood.md).

## System summary

Each device (**node**) runs an independent local model. Nodes running the same
model form a **cluster**. All nodes on the network form the **clique**. One node
is the **server node**: it holds shared contexts, the node registry, the router,
and the shared versioned DB. Every other node is a
**client node**. Every node is simultaneously a dev environment (jcode-style)
and a model provider, so all nodes are both servers and clients in the
inference sense.

```mermaid
flowchart TB
    subgraph clique [Clique - one LAN]
        S[Server node<br/>registry + router + contexts + vcs]
        subgraph clusterA [Cluster: qwen2.5-coder-7b]
            N1[Node 1]
            N2[Node 2]
        end
        subgraph clusterB [Cluster: chat model]
            N3[Node 3]
        end
    end
    N1 -- heartbeat/offer --> S
    N2 -- heartbeat/offer --> S
    N3 -- heartbeat/offer --> S
    S -- task assignment --> N1
    S -- task assignment --> N3
    U[User on any node] -- submit task --> S
```

## File map

| Path | Responsibility |
|---|---|
| `common/types.py` | Shared models: NodeInfo, ResourceSnapshot, ModelSpec, Task, CodeTaskSpec, Session, Cluster, permission levels. |
| `common/protocol.py` | Wire message schemas and (de)serialization: register, heartbeat, assign, result, context sync. |
| `common/config.py` | TOML config loading/validation for node and server roles. |
| `common/errors.py` | Error taxonomy shared by all components. |
| `common/think.py` | Reasoning/answer splitting for think-style models (`split_think`, streaming `StreamSplitter`) feeding the OpenAI-compat endpoint. |
| `common/tls.py` | TLS helpers. |
| `node/agent.py` | Node daemon lifecycle: start, join clique, serve, drain, leave. Also hosts the heartbeat loop for the MVP. |
| `node/discovery.py` | mDNS/zeroconf announce + browse; find or become server node. |
| `node/resources.py` | Probe CPU/GPU/memory/battery/load; produce ResourceSnapshot. |
| `node/model_runtime.py` | Adapter over llama-server/Ollama/vLLM (openai-compat): load, unload, infer, health. |
| `node/heartbeat.py` | Placeholder: heartbeat loop lives in `node/agent.py` for now. |
| `node/executor.py` | Task executor: single-shot streamed inference, plus the opt-in bounded tool loop (max 5 steps, server reverifies). |
| `node/tools.py` | Node-local tool sandbox: JSON tool calls (read/edit/write, allowlisted test runners), no shell for the model. |
| `scheduler/server.py` | Server-node entrypoint: compose registry, router, API, stores; serves installer, bundle, dash, chat, TUI. |
| `scheduler/registry.py` | Node registry, cluster membership, liveness, op-order tracking. |
| `scheduler/router.py` | Routing policy: eligibility, load/length-aware scoring, one task per node, leases, retries, first-commit-wins. |
| `scheduler/harness.py` | Deterministic code-edit harness: extract single diff fence, validate guards, build code prompt. |
| `scheduler/ledger.py` | Append-only sqlite ledger: one row per terminal task, accepted-work accounting per node. |
| `scheduler/tool_executor.py` | Server-side tool relay: executes worker-requested tool calls against server surfaces only (no shell, no network). |
| `scheduler/context_store.py` | Shared session contexts, intra-cluster shared context. |
| `scheduler/sessions.py` | Session lifecycle, rare cross-node session migration. |
| `scheduler/vcs.py` | Git-backed (dulwich) version control of server state and the canonical code-repo. |
| `scheduler/workspace.py` | Live realtime workspaces: sequenced in-memory patches, rebase, debounced git checkpoints, rehydrate. |
| `scheduler/workspaces.py` | Ephemeral per-task CODE_EDIT checkouts: seed, apply diff, run tests. |
| `scheduler/api/workspace_routes.py` | `/ws/workspace/{id}` realtime collab + `/v1/workspaces` REST (history, commits, flush). |
| `scheduler/suggestions.py` | Detect overloaded task types/nodes, suggest model changes. |
| `scheduler/api/rest.py` | HTTP API: sessions, nodes, tasks, code tasks + races, vcs, ledger, suggestions, OpenAI-compat `/v1/chat/completions` + `/v1/models`, web pages. |
| `scheduler/api/ws.py` | WebSocket event streams (dashboard, session watch, agent channel). |
| `client/sdk.py` | Python client for the server API (used by CLI, TUI, MCP server, dashboard). |
| `client/cli.py` | CLI: serve, join, onboard, join-remote, submit, code-submit, workspace, sessions, vcs, ledger, stats, dash, kick, mcp install/grow. |
| `client/home.py` | Button home TUI (`clique` with no args): Host, Join, Chat, Dash over the same CLI code paths. |
| `client/daemon.py` | Detached background process management for `clique serve`/`join` (pidfiles + logs). |
| `client/tui.py` | Fullscreen textual dashboard (`clique dash --full`) for monitor-less nodes (GX10). |
| `client/curl_tui.py` | Stdlib-only live TUI served at `/tui.py`; runs anywhere with python3 + curl. |
| `client/mcp_server.py` | `clique-mcp` stdio MCP server: chat, submit, code tasks, sessions, full workspace surface. |
| `client/mcp_install.py` | Auto-register `clique-mcp` into local harnesses (Claude Code, Codex, Cursor, Windsurf, Claude Desktop, Jcode). |
| `client/dashboard/README.md` | Web dashboard views and route spec. |
| `tests/README.md` | Test plan. |

## Library choices (open source unless inadequate)

| Concern              | Library                                                        | Why                                                                                                                                      | Alternatives considered                                                                                                             |
| -------------------- | -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Language/runtime     | Python 3.12 + `uv`                                             | Fast iteration.                                                                                                                  | Go (later, single-binary worker)                                                                                                    |
| LAN discovery        | `python-zeroconf` (mDNS/DNS-SD)                                | Pure-python, cross-platform, no daemon needed; standard for same-network discovery.                                                      | Avahi bindings (Linux-only), UDP broadcast (roll-your-own)                                                                          |
| Server API           | `FastAPI` + `uvicorn`                                          | Async HTTP + WebSocket in one framework, pydantic-native, OpenAPI for free.                                                              | Flask (no native WS), aiohttp (more manual)                                                                                         |
| Schemas/validation   | `pydantic` v2                                                  | Wire message validation, config validation, FastAPI integration.                                                                         | dataclasses + manual validation                                                                                                     |
| HTTP/WS client       | `httpx` + `websockets`                                         | Async, streaming responses for token streams.                                                                                            | requests (sync only)                                                                                                                |
| Resource probing     | `psutil`                                                       | CPU, memory, battery, load, per-process; cross-platform.                                                                                 | Platform-specific calls; note: GPU/VRAM needs per-platform extras (`nvidia-ml-py` on NVIDIA, `powermetrics`/IOKit parsing on macOS) |
| Model runtime        | `llama.cpp` (`llama-server`) primary; Ollama adapter secondary | GGUF, Metal + CUDA prebuilt binaries, OpenAI-compatible endpoint.                                               | vLLM (heavy for laptops), MLX (Mac-only)                                                                                            |
| Model download       | `huggingface_hub`                                              | Default model fetch (Qwen 2.5), resumable, revision pinning. Future best-model detection integrates here.                                | manual curl                                                                                                                         |
| Server DB            | `sqlite3` (stdlib), WAL mode                                   | Contexts, registry snapshots, ledger; single-writer fits one server node.                                                     | Postgres (overkill for LAN clique)                                                                                                  |
| Shared-DB versioning | `dulwich` (pure-python git)                                    | The requested "local .git in the shared db": snapshot/commit/diff/rollback without shelling out; no adequate non-git OSS beats git here. | `pygit2` (libgit2 build pain), GitPython (subprocess-based)                                                                         |
| CLI                  | `typer` + `rich`                                               | Headless config/dashboard alternative; `rich` tables for live status.                                                                    | argparse, click                                                                                                                     |
| Terminal dashboard   | `textual`                                                      | Full TUI dashboard for monitor-less nodes (GX10).                                                                                        | rich.live only                                                                                                                      |
| Web dashboard        | Static HTML + `htmx` (or Preact) served by FastAPI             | Zero build-step to start; upgrade path later.                                                                                            | React/Next (build overhead)                                                                                                         |
| Auth between nodes   | `PyNaCl` (libsodium) keypairs + token                          | Node identity, signed registration, op actions.                                                                                          | mTLS via self-signed CA (more setup)                                                                                                |
| Testing              | `pytest` + `pytest-asyncio`                                    | Standard.                                                                                                                                | —                                                                                                                                   |

## Implementation priority (from vision dump)

1. **P0 Connect the pool** — done: `node/discovery.py`, `node/agent.py`, `scheduler/registry.py`, `common/protocol.py`.
2. **P1 Basic router** — done: `scheduler/router.py` (longer prompts → bigger models, busyness-aware, one task per node), executor + heartbeat in `node/agent.py`, `node/model_runtime.py`.
3. **P2 Dashboard/UI** — done: `client/cli.py`, `client/dashboard/index.html` (served at `/dash`), `scheduler/api/*`, headless curl TUI.
4. **P2 Suggestions** — done: `scheduler/suggestions.py` (overloaded clusters → suggest an idle node switch models, with hysteresis).
5. **P3 Shared context** — done: `scheduler/context_store.py`, `scheduler/sessions.py` (intra-cluster now, inter-cluster far future).
6. **P3 Extras** — done: `scheduler/vcs.py` (dulwich state-repo + code-repo), live workspaces (`scheduler/workspace.py`), verified code-edit loop (`scheduler/harness.py`, `scheduler/workspaces.py`), append-only ledger, MCP server + auto-install, OpenAI-compat endpoint with reasoning split. Governance and cron were removed: collaborative dev env, any authenticated node may administrate.

## Cross-cutting invariants

- One task per node at a time (router-enforced; future task decomposition relaxes this).
- Session migration between nodes in a cluster is possible via server-held context but discouraged; router treats it as a last resort.
- Missing telemetry produces conservative routing, never fabricated numbers.
- Server node is a single point of coordination; durable state + git snapshots make it recoverable, not highly available.
