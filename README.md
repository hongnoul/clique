# clique — the personal AI computer, networked

**Claim: personal AI computers will be mainstream.** Boxes like the ASUS
Ascent GX10 put datacenter-class inference on a desk. clique is the
software layer that turns one of those boxes plus the laptops around it
into a next-generation development environment: every device is both a
model provider and a dev seat, work is scheduled across the pool, and
all sessions share one realtime version-controlled scope.

Status: dogfooded daily on a live ASUS Ascent GX10 server (vLLM FP8
Nemotron, 340+ TPS aggregate) plus a mixed fleet of Macs and Linux
laptops. See [docs/dogfood.md](docs/dogfood.md) for the live rollout.

## Three killer features

### 1. Socket realtime version control across sessions

Live workspaces are the clique's internal VCS: the server sequences
every patch over websockets, concurrent writers are rebased instead of
rejected, and debounced git checkpoints make history durable. Memory is
truth for seconds, git is truth for hours. Any number of humans and
agents, in any session, on any machine, edit the same live files and see
each other's writes in under a second. Details:
[docs/live-workspaces.md](docs/live-workspaces.md).

### 2. Scheduler: verified work across the pool

A durable sqlite-backed queue routes each task to the best idle node:
busyness- and size-aware scoring (longer prompts go to bigger models),
one task per node, leases that expire and retry on node loss, idempotent
submits, and first-commit-wins parallel races. For code edits, a
deterministic server-side harness extracts the model's diff, applies it
in an ephemeral checkout, runs the tests, and only then commits and
records the task as accepted in the ledger. Nodes are never trusted with
the canonical repo. Details: [CODE_EDIT_PLAN.md](CODE_EDIT_PLAN.md).

### 3. Open source harness integration

The clique meets existing agent tooling where it lives:

- **OpenAI-compatible API**: `/v1/chat/completions` and `/v1/models`, so
  any OpenAI client (jcode, opencode, Cline, plain SDKs) can point at
  the clique as a provider, including reasoning-token splitting for
  think-style models.
- **MCP server**: `clique-mcp` exposes chat, code tasks, sessions, and
  the full workspace surface (create/read/write/patch/history/flush/
  export/task) over stdio. `clique mcp install` auto-registers it into
  Claude Code, Codex CLI, Cursor, Windsurf, Claude Desktop, and Jcode.
- **Server-side tool relay**: models that emit OpenAI tool calls get
  them executed against the server's own surfaces (workspaces, sessions,
  vcs, stats) with no shell and no network on the node.

Every agent harness on every machine reads and writes the same sequenced
workspace state. That is the shared scope that makes a pool of personal
AI computers feel like one machine.

## The demo: ASUS Ascent GX10 as the home server

One GX10 runs the clique server plus a vLLM FP8 engine
(`nemotron-3-nano-fp8`, 16 parallel slots, 340+ TPS at 32-way batching,
see [dgx-vllm-endpoint.md](dgx-vllm-endpoint.md)). Laptops join over
LAN, tailnet, or a public quick tunnel with one command, contribute
their own local models, and every accepted task lands in the ledger.
The GX10 is headless: the CLI, stdlib TUI, and web dashboard were built
so a monitor-less box is a first-class citizen.

## What is implemented

mDNS discovery, signed registration (node keypair -> bearer token,
first client node is op), heartbeats, durable sqlite queue, busyness-
and size-aware routing, streaming results, retry on node loss,
cancellation, idempotent submits, sessions with cluster affinity and
migration, live workspaces (WS patch/delta/sync, rebase, git
checkpoints), verified code-edit loop with parallel races, node-local
bounded tool loop (server still reverifies), OpenAI-compat endpoint
with reasoning split, MCP server + auto-install, append-only ledger,
web dashboard at `/dash`, chat at `/chat`, curl-only snapshot at
`/dash.txt`, stdlib TUI at `/tui.py`, self-serving installer
(`/join.sh` + `/app.tgz` + `/repo.bundle`, no GitHub in the join path).

## Non-goal: income

This is a collaborative dev environment. The ledger tracks accepted
contributions per node (accountability and goodput visibility), not
earnings. There is no rate, payout, or settlement.

## Repo layout

```text
clique/
  README.md          # this file: product, quickstart
  SPEC.md            # implementation spec and file map
  CODE_EDIT_PLAN.md  # verified code-edit loop
  client/            # SDK, CLI, home TUI, MCP server, dashboard
  common/            # protocol, types, config, think-split, TLS
  scheduler/         # API, router, registry, ledger, harness, workspaces
  node/              # agent, discovery, runtime, executor, tool sandbox
  docs/              # dogfood, MCP, live workspaces
  deploy/            # public tunnel setup
  scripts/           # bootstrap, sweeps, docs-sync, vllm bench
```

## Getting started

Dogfooding the live server? Start with [docs/dogfood.md](docs/dogfood.md):
run `clique`, press Join, echo before GPU, one wave at a time.

```bash
git clone https://github.com/hongnoul/clique
cd clique
uv venv && uv pip install -e .          # or: pip install -e .
clique                                  # button home: Host Join Chat Dashboard
```

## Quickstart

```bash
uv venv && uv pip install -e .          # or: pip install -e .

# one screen, buttons call the CLI under the hood:
clique                                  # Host Join Chat Dashboard
# Host = start a server here, Join = join one (probes + detects runtime)

# headless / explicit (same paths the buttons call):
# terminal 1: server node (announces over mDNS)
clique serve --foreground

# terminal 2+: join devices. Echo runtime needs no model;
# point --runtime openai-compat at ollama/llama-server/vllm for real inference.
clique join --runtime echo --param-b 7 --name my-laptop
clique join --runtime openai-compat --model-name qwen2.5-coder:7b --param-b 7

# anywhere on the LAN
clique help                             # what every command does
clique nodes
clique submit "write a fizzbuzz in rust"
clique code-submit -p "fix the bug" -f main.py=./main.py --tools
clique workspace --create --files '{"main.py": "x = 1\n"}'
clique stats
clique dash --server http://<server-ip>:7777   # terminal dashboard + web link
```

The server node also serves the web UI: `<server>/dash` for the live
dashboard, whose menu leads to `<server>/chat` to ask the clique
something from a browser. `clique dash` prints the dashboard link, so
you never type an address.

### Join: one line, no token, no GitHub

The server serves its own installer plus its own source bundle, so
joining never touches GitHub, never needs a PAT, and never pastes a
secret. Auth to the clique happens after install via signed
registration (node keypair -> bearer token), handled silently by the
CLI. SSH/Tailscale is only hidden transport for headless boxes.

```bash
export CLIQUE=http://<server-ip>:7777   # or the public https URL (see below)
curl -s $CLIQUE/               # prints the one-line join
curl -fsSL --connect-timeout 5 $CLIQUE/join.sh | sh   # install, server preconfigured

# without installing anything (stdlib-only live TUI):
curl -fsSL $CLIQUE/tui.py | python3 - --server $CLIQUE
curl -s $CLIQUE/dash.txt       # snapshot, curl only

# every CLI command reads $CLIQUE_SERVER, so set once and forget flags:
export CLIQUE_SERVER=$CLIQUE
clique                       # button home: Host Join Chat Dashboard

# headless (same paths the buttons call):
clique join --runtime echo --param-b 7

# headless box over ssh (transport hidden, same join bundle):
clique join-remote gx10
```

### Anywhere: public URL, zero assumptions

For joiners outside your LAN/tailnet (no VPN, no tailscale, no
account), run a quick tunnel next to the server and share the https
URL it prints:

```bash
# on the server box (no account, no sudo): installs cloudflared,
# a user systemd unit, and prints the https URL
sh deploy/setup-tunnel.sh
# (which boils down to: cloudflared tunnel --url http://127.0.0.1:7777)
```

Write that URL to `~/.clique/public_url` (a `clique-tunnel.service`
user unit can own this) or set `$CLIQUE_PUBLIC_URL`, and the server
advertises it in `/` and `/v1/clique` as `public_url`. Nodes only dial
outbound, so the tunnel is all a stranger needs:

```bash
curl -fsSL --connect-timeout 5 https://<random>.trycloudflare.com/join.sh | sh
```

`clique` opens the button home (Host, Join, Chat, Dashboard, Stop,
Leave) when you have a tty; piped runs print `clique status` instead.
`clique help` prints what every command does. `clique dash` polls the
snapshot in a loop, leaves on `q`, and prints the web dashboard link
(`<server>/dash`, or the public https URL when a tunnel is up, so a
phone can open it too); `clique dash --full` opens the fullscreen
textual dashboard.

### Joining on enterprise Wi-Fi (eduroam, MIT SECURE, etc.)

Enterprise networks block mDNS, so set `CLIQUE_SERVER` once instead
of passing `--server` everywhere (see join section above).

Headless (no tty) notes: the served installer needs only curl, tar,
and python 3.11 plus. It unpacks the server's own `/app.tgz` bundle,
so there is no git clone, no credential prompt, and no GitHub token
anywhere in the path. `scripts/bootstrap.sh` remains for dev clones
only.
