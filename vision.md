# Vision: Idle Compute

**Plug in your idle laptop and get paid for every useful task it completes.**

Status: target for HackMIT 2026. This is a goal to prove, not a result already measured.

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

## Starting fleet and projected throughput

The network starts with 5 team laptops: 3 Macs plus 2 Arch or NVIDIA machines, each running one Qwen2.5-Coder-7B Q4 replica. No per token network hop. These are projections to verify in the benchmark sequence, not measured results.

- Single stream per replica: Mac about 40 to 60 tok/s, Arch about 15 to 25 tok/s. Pool total about 190 tok/s raw, about 150 tok/s committed after failed checks and retries.
- Loaded with 3 to 4 concurrent streams each: Mac about 25 tok/s, Arch about 10 tok/s. Pool total about 95 tok/s raw, about 70 to 80 tok/s committed goodput.
- Per request experience stays 15 to 50 tok/s depending on machine, which feels interactive for code tasks completing in seconds.

Aggregate committed goodput clears 50 tok/s with headroom. That aggregate number is the booth counter. Per stream above 50 sustained would need speculative decoding at 70 percent plus acceptance, a smaller model like Qwen3-4B, or Mac only replicas.

## Hackathon model plan

Primary model for the demo and the money math:

- Qwen2.5-Coder-7B-Instruct Q4: 1 node per replica, 5 replicas on 5 laptops.

Stretch model to prove larger than one laptop serving:

- Qwen3-30B-A3B MoE Q4: 2 nodes minimum, 3 recommended.
- DeepSeek-Coder-V2-Lite 16B Q4: 2 nodes.
- Qwen2.5-Coder-32B Q4: 3 nodes.
- Llama-3.3-70B Q4: 4 nodes, 1 left for coordinator.

Vision only, not for the weekend:

- DeepSeek V3 or V4 Flash class 600B plus Q4: 30 plus nodes minimum.

## The money goal

Fixed sponsored rate for the hackathon: **$0.20 per accepted coding task** on the 7B primary model.

Sponsor pool: **$50**. At 60 to 100 accepted tasks per hour across 5 laptops, the pool lasts a 3 hour demo and pays about $6 to $10 per laptop.

Monthly projection: 4 accepted tasks per hour over 8 idle hours per night is about 1000 tasks per month, which is **$200 per month per laptop**. Display this as a run rate projection from live tasks times rate. Do not describe demo credits as cash earnings until payout consent, eligibility, provider terms, and settlement handling are established.

The throughput math is reasonable. The open business question is sustained demand at $0.20 when datacenter 7B inference is cheaper per token. The answer for the pilot is to sell verified task completion with tests, not raw tokens.

### Mining reward curve

Pay for useful work first, then boost early and loyal nodes from a separate bonus pool so the base rate never breaks.

- Base pay: $0.20 per accepted task, same for every node, always.
- Early join boost: first 10 nodes earn 2x base for their first 50 accepted tasks. Next 20 nodes earn 1.5x for their first 50. Everyone after earns base. Early rank is fixed by first accepted task, not by join page load.
- Loyalty multiplier: continuous serving time adds up to 1.5x. Every 30 minutes of sustained goodput within target adds 0.1x, capped at 0.5x extra. Leaving or pausing freezes the multiplier. Failing readiness resets the current 30 minute window, not the whole history.
- Reliability bonus: nodes with zero dropped attempts over a rolling hour earn an extra $0.05 per accepted task that hour.

Example: a judge joins as node 7, serves 3 hours with no drops, and completes 25 tasks. That is 25 times $0.20 times 2.0 early times about 1.3 loyalty, plus 25 times $0.05 reliability, or about $14.25 from the bonus pool. A node joining 2 hours later doing the same work earns roughly half. Both see the exact multiplier math on screen before they join.

Fund the boost from a separate $25 bonus pool on top of the $50 base pool. The ledger settles base pay per task and bonus pay per multiplier event as separate intents, so auditors can replay every cent. Display base earnings and bonus earnings as separate lines, and label the monthly $200 projection as base only.

## Anyone at HackMIT can join live

Any hacker or judge can become a paid node during the demo. Open the join page, download one signed worker binary, and run it. The worker benchmarks the laptop, reports model readiness and resource limits, and advertises capacity only after loading and warmup checks pass. No inbound ports and no env key plumbing: the worker dials out to the coordinator over authenticated TLS.

Joiners pick the 7B replica path by default. Strong stable machines may be offered a pipeline shard role. Owners set CPU and memory caps, keep pause and quit, and default to no battery inference. Leaving mid job triggers a retry elsewhere with no duplicate committed result, and the ledger shows the join and the recovery.

Each new laptop appears on the booth screen within a minute as added goodput and a share of the $0.20 per task pool. Judges do not have to trust a slide. They add their own machine and watch queue delay fall.

## What must be shown live

1. Visitor submits a job and sees queue position.
2. Multiple laptops complete independent tasks concurrently.
3. One laptop gets busy or leaves and the scheduler adapts without duplicate committed results.
4. A node rejoins and goodput recovers.
5. The ledger shows completed tasks times $0.20 with per laptop totals plus failures and wasted work.

## Beyond the weekend

Keep the durable ledger, fair queue, recovery, and fixed rate settlement. Add pipeline replicas for the 30B MoE class. Add pipeline sharding toward 70B only after the 7B money loop is stable. Pursue V4 Flash class serving only with 30 plus stable nodes and measured hop latency.

Details: [Scheduler architecture](docs/architecture.md) · [Evidence and acceptance plan](docs/validation.md)
