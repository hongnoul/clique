# Parallelism cross-sections for tcj on heterogeneous consumer nodes

Swarm research synthesis, 4 workers (replica horizontal, model split, embedding + scheduling, spec decode + network). 2026-09-19.

## 1. Verdict matrix

| Cross-section | Viable for tcj? | Network need | Scaling shape | Role |
|---|---|---|---|---|
| Replica horizontal (inference) | Yes, primary | KBs per job, works over DERP/CF Tunnel | Linear in goodput, not per-token speed | Money path |
| Embedding horizontal | Yes, easiest add | KBs per chunk, fully async | Near-linear (no inter-node comms) | Second revenue cross-section |
| Batch test execution / verification | Yes | Tiny | Near-linear, self-verifying | Uses CPU-only nodes |
| Pipeline sharding (32B/70B) | Stretch only | Direct WireGuard, <5 ms RTT, ~100 Mbps, stable nodes | Enables bigger models; throughput via multi-request pipelining | Demo flex, not money |
| Cross-node speculative decoding | No | RTT < ~35 ms AND slow target | Negative-to-tiny budget on your hardware | Run SD locally instead |
| Tensor parallel across nodes | Never | <0.5 ms RTT, multi-Gbps | ~160 allreduce rounds/token for 70B = 0.5–1.6 s/token from RTT alone | Ruled out |

## 2. Replica tier (Worker A: retriever)

- One `llama-server` per node. Slots by RAM tier: 8 GB → `-np 2`, 16 GB → 3–4, 32 GB → 4–6. `-c = slots × 8192`. q8_0 KV cache on tight nodes.
- Qwen2.5-Coder-7B uses GQA: KV is only ~0.44 GiB per 8k stream, so the 2–4 GB estimate in ARCHITECTURE.MD is conservative. More slots fit than the docs assume.
- Routing under hardware variance: no production system uses static hardware scores. paddler (llama.cpp LB) does slot accounting; SGLang/vLLM routers use load-aware / power-of-two-choices. Recommendation: PoT choices over estimated finish time, using per-node `{prefill, decode[batch]}` envelopes measured at join benchmark.
- Batching wins: 3–4 concurrent slots gives ~1.7–2x aggregate on GPU nodes (decode is memory-bandwidth-bound) whenever queue depth ≥ 2 per node. CPU nodes plateau at 1–2 slots.
- Failure economics: replica churn cost is linear (lose one node's attempts, retry elsewhere). Pipeline group failure loses all in-flight work in the group.
- Caution: vision.md's "Mac 40–60 tok/s" only holds for M-series Pro/Max. Base M1/M2/M3 do ~14–24 tok/s on 7B Q4 (official llama.cpp benchmark table). Re-base pool projections on actual team hardware.

## 3. Model-split tier (Worker B: pawprint)

- Tensor parallelism: infeasible over WiFi/VPN. ~160 synchronous allreduces per token (70B class) means RTT alone contributes 0.5–1.6 s/token. distributed-llama is TP-based with 2^n node counts, so it inherits this.
- Pipeline parallelism: per-hop activation is one hidden vector, ~7 KB (7B), ~10 KB (32B), ~16 KB (70B) f16 per token. Decode is latency-bound not bandwidth-bound. Prefill IS bandwidth-bound: 1000-token prefill ships 7–16 MB per boundary, ~0.6–1.3 s per hop on 100 Mbps WiFi.
- llama.cpp RPC: works today with mixed Metal+CUDA (measured ~17–18 tok/s for a 27B split over 2.5GbE), but is officially proof-of-concept: unauthenticated, slow weight-transfer bugs, needs Tailscale-only exposure and LAN latency.
- prima.cpp (MIT license, llama.cpp fork) is the strongest heterogeneity-aware option: speed-proportional layer assignment, auto-drops slow nodes, 70B at ~1.5 tok/s and 32B at ~26 tok/s on 4 weak home devices. exo pivoted Mac/RDMA-focused. Petals/Cake poor fits.
- Bottleneck law: pipeline throughput = slowest stage. Heterogeneous assignment must be proportional to measured node speed, and one slow/DERP-relayed node poisons the group. Fill the pipe with concurrent requests to recover aggregate throughput.

## 4. Embedding + embarrassingly parallel tier (Worker C: piglet)

- Embedding models are tiny (64 MB–1.4 GB: bge, nomic-embed, gte, e5) vs 4.7 GB for the 7B, so they co-load next to a replica or run on nodes too weak for 7B.
- Throughput asymmetry is huge: ~14k passages/s (nomic on M-Max class via MLX) vs <1 req/s on weak CPU nodes. Corpus sharding is near-linear because chunks are independent, zero inter-node traffic.
- Second-best add: sandboxed test execution. Self-verifying (exit codes), CPU-only nodes are productive, and tcj already needs the sandbox for the accept gate.
- Scheduling under ~5–30% spec variance: pull-based dynamic chunk queue (work stealing), NOT static proportional splits. Chunk size calibrated from join benchmark, plus tail hedging for stragglers. This is BOINC's proven volunteer-computing pattern (adaptive replication, homogeneous redundancy, credit per validated unit).
- Verification caveat: embeddings are deterministic per (model, quant, backend) but NOT across Metal/CUDA/CPU. Validate with cosine-similarity tolerance on 1–5% sampled chunks, never bitwise cross-backend comparison. Same logic applies to redundant-execution validation of inference outputs.

## 5. Speculative decoding + network reality (Worker D: bonehound)

- Distributed speculative decoding exists in the literature (DSD arXiv:2511.21669, PipeSD ICML'26) but gains are modest (1.1–2.16x in edge-cloud settings) and "Speculation at a Distance" (arXiv:2606.25091) proves co-located SD strictly dominates synchronous distributed SD when one machine can host both models.
- Acceptance math for tcj: with γ=4 drafts, α=0.7 acceptance → ~2.77 committed tokens/round. RTT budget ≈ 35–40 ms only when the target is slow (20 tok/s). At Mac speeds (40–60 tok/s) the budget is negative even at 0 RTT.
- Since a 0.5–1B draft model fits on every tcj laptop, local SD dominates in all cases: `llama-server -md/--model-draft`, zero custom code. llama.cpp has no network drafting API anyway (draft loads in-process; RPC backend only moves layers).
- Naive remote drafting also ships ~300 KB/token of vocab logits unless token-ID-only greedy verification is used.
- Network baselines: Tailscale direct WireGuard LAN 1–5 ms; DERP relay 37–280 ms with severe jitter; venue WiFi adds 5–50 ms jitter + loss; Cloudflare Tunnel is coordinator-ingress only, worker↔worker cannot transit it.

## 6. Recommended architecture

Three-tier, heterogeneity-aware, all behind the one coordinator:

1. **Tier 1, replicas (money path):** every node runs the 7B whole with local speculative decoding (small draft model in-process) to chase >50 tok/s per stream. Slot count by RAM tier, PoT-choices routing over measured envelopes, bounded retry on churn.
2. **Tier 2, embarrassingly parallel batch (embeddings, test execution):** pull-based chunk queue, dynamic sizing, sampled redundant validation with tolerance-based comparison. Absorbs weak/CPU-only joiners that would drag Tier 1.
3. **Tier 3, pipeline groups (stretch demo):** only among team-fleet nodes with verified direct WireGuard <5 ms RTT and stable AC power. llama.cpp RPC or prima.cpp for 32B (2–3 strong nodes). Admission-time link check, pinned builds, pre-staged weights, multi-request pipelining. Never admit a DERP-relayed or battery node into a group.

Node classification at join benchmark decides tier eligibility: `{decode envelope, RAM, link type (direct vs relayed), RTT to coordinator/peers, AC state}`.

Explicit non-goals: tensor parallelism across nodes, cross-node remote drafting, seamless mid-stream migration.

## 7. Doc corrections to make

- vision.md Mac single-stream numbers assume Pro/Max chips; base M-series is ~14–24 tok/s. Re-base the 190 tok/s pool projection.
- ARCHITECTURE.MD KV estimate (2–4 GB per replica) is ~5x conservative for GQA 7B; slot counts can be higher.
- ARCHITECTURE.MD "experimental remote drafting" section should be demoted to local-only SD per the RTT math.
