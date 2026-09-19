# tcj — Idle Compute

**Turn independently owned, intermittently idle laptops into one dependable inference service.**

Status: HackMIT 2026 implementation repo. Proposal draft ported from `hackmit-prep` on 2026-09-15. No service is implemented and no performance improvement has been measured.

This repo is where the demo gets built. Design docs live in `hackmit-prep` until promoted here.

## The idea

People contribute spare compute from hardware they already own. Consumers submit inference requests through one interface. Providers earn for useful work, while the network handles placement, concurrency, interruptions, verification, and accounting.

The central contribution is the **scheduler and node-capacity protocol**. It assesses each host, determines what it can safely contribute, and coordinates heterogeneous machines so consumers do not have to think about the underlying laptops.

The product is not another chatbot, a GPU rental directory, or a requirement to buy a mining rig. Chat and API clients are interfaces to the same shared inference service.

> The network should get more useful as people join, without making consumers manage more infrastructure.

## What we are betting on

A sufficiently dependable pool of existing consumer hardware could become a credible alternative for a defined subset of commercial AI workloads. It does not have to beat every frontier model to be useful, but it must deliver something customers value at viable cost and acceptable quality.

The hypothesis is that good coordination can recover enough otherwise-unused capacity to make that service worthwhile. This is not yet evidence that it will be cheaper, greener, or as fast as an established API.

### Non-negotiables

- **No required hardware purchase for providers.** A watt meter is optional benchmark equipment, not part of joining.
- **The owner stays in control.** Participation is opt-in, with resource limits, schedules, pause, and uninstall. Default to no battery-powered inference.
- **Multiple consumers can submit concurrently.** Durable asynchronous jobs are a first-class interface. Streaming is a delivery option.
- **Distribution is central.** Optimize communication within the network, not the product into a single-machine solution.
- **Useful output matters.** Rejected drafts, duplicate retries, and incompatible generated code are not successful work.
- **Promises match evidence.** No frontier-parity, uninterrupted-failover, privacy, earnings, or carbon claim without the corresponding proof.

## The scheduler is the product

It must make a changing pool of machines behave like a predictable service:

| Responsibility | What it means |
|---|---|
| Discover capacity | Measure supported models, memory headroom, sustainable throughput, connectivity, and owner activity. |
| Admit work | Accept only within explicit queue, resource, and request limits. Do not confuse instant acknowledgement with instant completion. |
| Place work | Choose a warmed replica, a compatible cooperative model group, or an eligible helper pair. |
| Coordinate concurrency | Batch compatible requests, reuse cached prefixes, and allocate capacity fairly across consumers. |
| Handle change | Withdraw capacity, drain work where possible, and recover interrupted jobs with explicit attempt tracking. |
| Account for work | Commit one authoritative result and one settlement intent under a stated payout policy. |

It schedules **usable model-serving capacity**, not an undifferentiated sum of RAM and CPU cores. A warmed replica is not equivalent to several laptops that could potentially download and load model shards.

## How machines cooperate

The network should select the appropriate form of parallelism rather than putting every machine in every token's critical path.

1. **Replica groups:** independent workers serve different requests, with batching within each worker. This is the initial path to multi-user throughput.
2. **Cooperative model groups:** stable, compatible nodes host fragments of a model. Evaluate both larger-model capability and throughput with multiple requests in flight.
3. **Experimental helper pairs:** a small model proposes token blocks and a target model verifies them. Remote speculative decoding must demonstrate a gain after drafting and communication costs.
4. **Parallel task graphs:** one coding job can contain independent subtasks that run across nodes, with integration and checking included in the completion time.

These modes are not automatically composable. In particular, the checked stock MLX server disables its batchable path when a draft model is loaded and rejects draft models in its distributed mode.

## A concrete first workload

Use a public demo repository with a set of independent functions, supplied interfaces, and predefined tests. Consumers request implementations, tests, or documentation through the service.

The visible result is a feed of generated patches and checked task outcomes, not just a large token counter. Generated code runs only in an isolated evaluation environment and is not automatically merged into the control plane.

Seeded demand is acceptable if disclosed: the team can sponsor real tasks and show their outputs. It is not evidence of organic customer demand. A credits-only ledger must not be described as cash earnings.

## Hackathon scope

### Core demonstration

- One coordinator, a durable job ledger, and a consumer interface.
- A small number of existing laptops acting as independently interruptible workers.
- One selected model/task class before attempting multi-model routing.
- Capacity advertisements, admission limits, placement, fair queues, and recovery from a lost worker.
- Concurrent consumers submitting real jobs and seeing status, results, and transparent accounting.
- A matched single-node versus pool benchmark, including foreground activity on a participating laptop.

### Stretch, not required to make the core real

- Model-fragment placement across a stable group.
- Remote speculative decoding and measured adaptive role selection.
- Wider public participation after the join path and abuse controls work.
- Real payouts, only after budget, consent, eligibility, provider terms, and settlement handling are established.

The prototype uses a coordinator. Distributed worker ownership does not require claiming a fully decentralized control plane. Removing that coordinator as a single point of failure is subsequent work.

## The booth

Show one service receiving requests from several people while the underlying machines change availability.

1. A visitor submits a job and sees its queue position or estimated start, with uncertainty disclosed.
2. Multiple nodes work on independent requests or eligible parts of that job.
3. A participant resumes using a laptop. Its advertised capacity drops and the scheduler responds.
4. A worker disappears. The affected job retries or reports interruption without duplicate committed results.
5. A node joins or becomes idle again. Compare useful service capacity before and after.

Display completed tasks, **goodput within the declared service target**, queue delay, per-request latency, and aggregate delivered output TPS. Show failures and wasted work as well as successes. Public joiners are real nodes only after loading and readiness checks, not immediately after opening a web page.

## Repo layout

```text
tcj/
  README.md          # this file, implementation overview
  coordinator/       # TODO: API, admission, scheduler, ledger
  worker/            # TODO: capacity agent, inference backend
  docs/              # TODO: promote architecture.md and validation.md from hackmit-prep
  vision.md          # TODO: promote from hackmit-prep if needed
```

## Getting started

```bash
git clone https://github.com/hongnoul/tcj
cd tcj
# TODO: add setup, run coordinator, join worker
```

Current state is docs only. Do not claim a running service until the acceptance checks in `docs/validation.md` pass.

## Category fit and economics

**Sustainability is a candidate framing, not an automatic property.** Reusing existing hardware avoids a required new purchase, but inference still consumes additional electricity. Payment can also induce extra usage or hardware purchases. Measure whole-system energy for comparable useful output, including retries and coordination, before claiming savings.

Do not choose hardware or distort the product solely to qualify for a sponsor prize. Current track eligibility, judging rules, and sponsor challenges need official confirmation.

For the prototype, separate scheduling from pricing and settlement. A disclosed fixed rate per defined task/model class is easier to audit than an invented token market. Crypto or fiat can be a later settlement choice. Neither solves compute pricing or output verification by itself.

The scheduler is an allocator, not automatically a market maker. It cannot create supply when all providers leave. A viable business needs positive unit economics and actual demand, not just a working distributed demo.

## Next decisions

1. Confirm the exact five laptop configurations and supported runtime/model combinations.
2. Choose the initial task class, model, context limits, and quality checks.
3. Set acceptance targets for latency, owner impact, and recovery before benchmarking.
4. Implement the durable multi-user serving loop before advanced cooperative modes.
5. Prove that adding nodes improves a fixed workload, then investigate single-request acceleration.

Design source: `~/git/hackmit-prep` — `README.md`, `vision.md`, `docs/architecture.md`, `docs/validation.md`
