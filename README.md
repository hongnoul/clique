# clique — Idle Compute

**Plug in your idle laptop and get paid for every useful task it completes.**

Status: target for HackMIT 2026. This is a goal to prove, not a result already measured.

Details: [Code edit plan](CODE_EDIT_PLAN.md).

## The product in one paragraph

Consumers submit batch inference jobs through one API. A scheduler places each job on idle laptops supplied by independent owners. Results return through the same API like any other provider. The backend is consumer hardware, not a datacenter.

Demand is batch code work that is easy to verify with tests. Supply is existing laptops. The interface is OpenAI compatible plus durable async jobs.

## Why this is worth building

In 2026 there is no gap between a hackathon demo and a business if the demo serves real jobs with real accounting. If the ledger records useful work and pays a fixed rate from day one, the weekend prototype is also a paid pilot.

The bet is that idle consumer hardware becomes the cheapest compliant layer for delay tolerant work. Datacenters win on single stream speed. A laptop pool wins on zero new hardware, concurrent batch throughput, and owner aligned supply.

## Why it is not absurdly slow

Use replica groups first. Each laptop runs a whole small model locally, so there is no per token network hop. One request runs at single laptop speed. That is slower than a datacenter but acceptable for delay tolerant code tasks that complete in seconds.

Pipeline sharding is a stretch path for models no single laptop can hold. It adds hop latency per token but keeps per hop payloads small and gains throughput by filling the pipe with many requests.

## Why more nodes is better

More warmed replicas means more concurrent jobs, lower queue delay, and higher goodput within the declared service target. Reliability also improves because a lost worker triggers a retry on another node instead of a failed job.

Single request speed does not improve by adding replicas. Claim throughput and availability gains, not per token acceleration.

## Starting fleet

The network starts with 5 team laptops: 3 Macs plus 2 Arch or NVIDIA machines, each running one Qwen2.5-Coder-7B Q4 replica. No per token network hop. Throughput numbers below are projections to verify, not measured results.

Primary model: Qwen2.5-Coder-7B-Instruct Q4, 1 node per replica, 5 replicas. Stretch: Qwen3-30B-A3B MoE Q4, DeepSeek-Coder-V2-Lite 16B, Qwen2.5-Coder-32B, Llama-3.3-70B across 2 to 4 nodes.

## Non-goal: income

This is a collaborative dev environment. The ledger tracks accepted
contributions per node (accountability and goodput visibility), not
earnings. There is no rate, payout, or settlement.

## Anyone at HackMIT can join live

Any hacker or judge can become a node during the demo. Open the join page, download one signed worker binary, and run it. The worker benchmarks the laptop, reports model readiness and resource limits, and advertises capacity only after loading and warmup checks pass.

Each new laptop appears on the booth screen within a minute as added goodput.

## What must be shown live

1. Visitor submits a job and sees queue position.
2. Multiple laptops complete independent tasks concurrently.
3. One laptop gets busy or leaves and the scheduler adapts without duplicate committed results.
4. A node rejoins and goodput recovers.
5. The ledger shows accepted tasks with per laptop totals plus failures and wasted work.

## Beyond the weekend

Keep the durable ledger, fair queue, and recovery. Add pipeline replicas for the 30B MoE class. Add pipeline sharding toward 70B only after the 7B loop is stable.

## Repo layout

```text
clique/
  README.md          # this file: product, quickstart
  CODE_EDIT_PLAN.md  # verified code-edit loop plan
  client/            # SDK, CLI, TUI, MCP server, dashboard
  common/            # protocol, types, config, TLS
  scheduler/         # API, router, registry, ledger, workspaces
  node/              # agent, discovery, runtime, executor
  docs/              # dogfood, MCP, live workspaces
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

## MVP quickstart

```bash
uv venv && uv pip install -e .          # or: pip install -e .

# one screen, buttons call the CLI under the hood:
clique                                  # Host Join Chat Dashboard
# Host = start a server here, Join = join one (probes + detects runtime)

# headless / explicit (same paths the buttons call):
# terminal 1: server node (announces over mDNS)
clique serve --foreground

# terminal 2+: join devices. Echo runtime needs no model;
# point --runtime openai-compat at ollama/llama-server for real inference.
clique join --runtime echo --param-b 7 --name my-laptop
clique join --runtime openai-compat --model-name qwen2.5-coder:7b --param-b 7

# anywhere on the LAN
clique help                             # what every command does
clique nodes
clique submit "write a fizzbuzz in rust"
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

Implemented: mDNS discovery, signed registration (first client node is op),
heartbeats, durable sqlite queue, busyness- and size-aware routing (longer
prompts to bigger models, one task per node), streaming results, retry on
node loss, cancellation, idempotent submits, CLI, sessions, live
workspaces, MCP server, dashboard. See CODE_EDIT_PLAN.md for the
verified code-edit loop and docs/ for the dogfood guides.

### Joining on enterprise Wi-Fi (eduroam, MIT SECURE, etc.)

Enterprise networks block mDNS, so set `CLIQUE_SERVER` once instead
of passing `--server` everywhere (see join section above).

Headless (no tty) notes: the served installer needs only curl, tar,
and python 3.11 plus. It unpacks the server's own `/app.tgz` bundle,
so there is no git clone, no credential prompt, and no GitHub token
anywhere in the path. `scripts/bootstrap.sh` remains for dev clones
only.
