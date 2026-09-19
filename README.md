# tcj — Idle Compute

**Plug in your idle laptop and get paid for every useful task it completes.**

Status: target for HackMIT 2026. This is a goal to prove, not a result already measured.

This README restates [vision.md](vision.md). Details: [Vision](vision.md) · [Scheduler architecture](ARCHITECTURE.MD) · [Research](research/README.md).

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

The network starts with 5 team laptops: 3 Macs plus 2 Arch or NVIDIA machines, each running one Qwen2.5-Coder-7B Q4 replica. No per token network hop. Throughput numbers in [vision.md](vision.md) are projections to verify, not measured results.

Primary model: Qwen2.5-Coder-7B-Instruct Q4, 1 node per replica, 5 replicas. Stretch: Qwen3-30B-A3B MoE Q4, DeepSeek-Coder-V2-Lite 16B, Qwen2.5-Coder-32B, Llama-3.3-70B across 2 to 4 nodes. See [vision.md](vision.md) for the full model plan.

## The money goal

Fixed sponsored rate: **$0.20 per accepted coding task** on the 7B primary model. Sponsor pool **$50** lasts a 3 hour demo at 60 to 100 accepted tasks per hour. Monthly projection is about **$200 per month per laptop** at 4 tasks per hour over 8 idle hours per night. Display as run rate projection, not cash earnings, until payout consent and settlement handling are established.

Early and loyal nodes earn a boost from a separate $25 bonus pool: 2x for the first 10 nodes, 1.5x for the next 20, loyalty up to 1.5x, plus $0.05 per task for zero-drop hours. See [vision.md](vision.md) for the exact curve.

## Anyone at HackMIT can join live

Any hacker or judge can become a paid node during the demo. Open the join page, download one signed worker binary, and run it. The worker benchmarks the laptop, reports model readiness and resource limits, and advertises capacity only after loading and warmup checks pass.

Each new laptop appears on the booth screen within a minute as added goodput and a share of the per task pool.

## What must be shown live

1. Visitor submits a job and sees queue position.
2. Multiple laptops complete independent tasks concurrently.
3. One laptop gets busy or leaves and the scheduler adapts without duplicate committed results.
4. A node rejoins and goodput recovers.
5. The ledger shows completed tasks times $0.20 with per laptop totals plus failures and wasted work.

## Beyond the weekend

Keep the durable ledger, fair queue, recovery, and fixed rate settlement. Add pipeline replicas for the 30B MoE class. Add pipeline sharding toward 70B only after the 7B money loop is stable.

## Repo layout

```text
tcj/
  README.md          # this file, restatement of vision.md
  vision.md          # full vision with throughput and money math
  ARCHITECTURE.MD    # scheduler and node-capacity protocol
  research/          # parallelism, frameworks, DGX Spark, architecture recommendation
  client/            # TODO: consumer API client
  scheduler/         # TODO: API, admission, scheduler, ledger
  node/              # TODO: capacity agent, inference backend
```

## Getting started

```bash
git clone https://github.com/hongnoul/tcj
cd tcj
# TODO: add setup, run coordinator, join worker
```

## MVP quickstart

```bash
uv venv && uv pip install -e .          # or: pip install -e .

# terminal 1: server node (announces over mDNS)
clique-server

# terminal 2+: join devices. Echo runtime needs no model;
# point --runtime openai-compat at ollama/llama-server for real inference.
clique join --runtime echo --param-b 7 --name my-laptop
clique join --runtime openai-compat --model-name qwen2.5-coder:7b --param-b 7

# anywhere on the LAN
clique nodes
clique submit "write a fizzbuzz in rust"
clique stats
clique dash --server http://<server-ip>:7777   # live terminal dashboard
```

### Headless access: zero-install curl flow

For monitor-less nodes (GX10 over SSH) with no install yet, the server
itself serves everything as plain text. Pick the lightest option that works:

```bash
export CLIQUE=http://<server-ip>:7777
curl -s $CLIQUE/               # prints this menu
curl -s $CLIQUE/dash.txt       # snapshot, curl only, no python needed
watch -n 2 curl -s $CLIQUE/dash.txt   # live loop, curl + watch only

# live fullscreen TUI, stdlib-only (no pip, no clone):
curl -fsSL $CLIQUE/tui.py -o /tmp/clique-tui.py
python3 /tmp/clique-tui.py --server $CLIQUE

# one-liner into python3, or snapshot-once mode for pipes/cron:
curl -fsSL $CLIQUE/tui.py | python3 - --server $CLIQUE
curl -fsSL $CLIQUE/tui.py | python3 - --server $CLIQUE --once

# full CLI install (pinned to that server). This repo is private, so
# export a fine-grained PAT (contents:read on hongnoul/tcj) first or
# the clone step aborts with "terminal prompts disabled":
export CLIQUE_GITHUB_TOKEN=github_pat_...
curl -fsSL $CLIQUE/join.sh | sh
```

After install, `clique dash --server $CLIQUE` polls the same snapshot
in a loop; `clique dash --full` opens the fullscreen textual TUI
(Devices, Queue, Sessions, Governance tabs) when you have a tty.

Implemented: mDNS discovery, signed registration (first client node is op),
heartbeats, durable sqlite queue, busyness- and size-aware routing (longer
prompts to bigger models, one task per node), streaming results, retry on
node loss, cancellation, idempotent submits, CLI. See SPEC.md for what is
next (sessions, permissions UI, cron, vcs, dashboard).

### Joining on enterprise Wi-Fi (eduroam, MIT SECURE, etc.)

Enterprise networks block mDNS, so always pass `--server` explicitly:

```bash
clique join --server http://<server-ip>:7777 --runtime echo --param-b 7
```

If `curl http://<server-ip>:7777/v1/clique` times out, the network isolates
clients from each other. Use a hotspot instead: the server machine (or a
phone) opens a hotspot, everyone joins it, and the server IP is typically
`192.168.2.1` (Mac Internet Sharing) or the phone's gateway IP.

One-line install on a new device (this repo is private: create a
fine-grained PAT with contents read on `hongnoul/tcj`, then fetch the
installer with the token so both the script download and the clone authenticate):

```bash
export CLIQUE_GITHUB_TOKEN=github_pat_...
curl -fsSL -H "Authorization: Bearer $CLIQUE_GITHUB_TOKEN" \
  https://raw.githubusercontent.com/hongnoul/tcj/main/scripts/bootstrap.sh | sh
```

Headless (no tty) notes: the installer sets `GIT_TERMINAL_PROMPT=0` and SSH
`BatchMode` so git fails fast instead of hanging on a credential prompt. The
token is sent as an `Authorization` header, never embedded in the remote URL.
If the clone fails, the script prints diagnostics plus a `contents:read`
tarball fallback. No token and no other GitHub credentials means the clone
aborts with `could not read Username: terminal prompts disabled`.
