# tcj — Idle Compute

**Plug in your idle laptop and get paid for every useful task it completes.**

Status: target for HackMIT 2026. This is a goal to prove, not a result already measured.

Details: [Spec](SPEC.md).

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

## The money goal

Fixed sponsored rate: **$0.20 per accepted coding task** on the 7B primary model. Sponsor pool **$50** lasts a 3 hour demo at 60 to 100 accepted tasks per hour. Monthly projection is about **$200 per month per laptop** at 4 tasks per hour over 8 idle hours per night. Display as run rate projection, not cash earnings, until payout consent and settlement handling are established.

Early and loyal nodes earn a boost from a separate $25 bonus pool: 2x for the first 10 nodes, 1.5x for the next 20, loyalty up to 1.5x, plus $0.05 per task for zero-drop hours.

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
  README.md          # this file: product, money goal, quickstart
  SPEC.md            # implementation spec
  client/            # consumer API client, CLI, curl TUI
  scheduler/         # API, admission, scheduler, ledger
  node/              # capacity agent, inference backend
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

### Join: one line, no token, no GitHub

The server serves its own installer plus its own source bundle, so
joining never touches GitHub, never needs a PAT, and never pastes a
secret. Auth to the clique happens after install via signed
registration (node keypair -> bearer token), handled silently by the
CLI. SSH/Tailscale is only hidden transport for headless boxes.

```bash
export CLIQUE=http://<server-ip>:7777
curl -s $CLIQUE/               # prints the one-line join
curl -fsSL $CLIQUE/join.sh | sh   # install, server preconfigured

# without installing anything (stdlib-only live TUI):
curl -fsSL $CLIQUE/tui.py | python3 - --server $CLIQUE
curl -s $CLIQUE/dash.txt       # snapshot, curl only

# every CLI command reads $CLIQUE_SERVER, so set once and forget flags:
export CLIQUE_SERVER=$CLIQUE
clique dash                    # live terminal dashboard
clique join --runtime echo --param-b 7

# headless box over ssh (transport hidden, same join bundle):
clique join-remote gx10
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

Enterprise networks block mDNS, so set `CLIQUE_SERVER` once instead
of passing `--server` everywhere (see join section above).

Headless (no tty) notes: the served installer needs only curl, tar,
and python 3.11 plus. It unpacks the server's own `/app.tgz` bundle,
so there is no git clone, no credential prompt, and no GitHub token
anywhere in the path. `scripts/bootstrap.sh` remains for dev clones
only.
