# DGX Spark + Heterogeneous Scheduling Research (tcj)

Date: 2026-09-19. Fleet context: 5 laptops (3 Macs + 2 Arch/NVIDIA) running Qwen2.5-Coder-7B Q4 replicas, no per-token network hop. Projections: ~150 tok/s committed single-stream pool, ~70-80 tok/s loaded. One NVIDIA DGX Spark joins as a sixth, very different node.

## 1. DGX Spark specifics

Source: NVIDIA DGX Spark product page and spec table (https://www.nvidia.com/en-us/products/workstations/dgx-spark/).

- Superchip: GB10 Grace Blackwell. CPU: 20-core Arm (10x Cortex-X925 + 10x A725).
- GPU: Blackwell, 5th-gen Tensor Cores, up to 1 PFLOP FP4 (footnote: with sparsity, so ~500 TFLOPS dense FP4, ~250 TFLOPS FP8 effective).
- Memory: 128 GB LPDDR5x coherent unified memory, 256-bit interface, 273 GB/s.
- Storage: 4 TB NVMe. Networking: ConnectX-7 200 Gb/s NIC plus 10 GbE, WiFi 7.
- Power: 140 W chip TDP, 240 W supply. OS: DGX OS. Size: 150x150x50.5 mm, 1.2 kg.
- NVIDIA workload claims: inference up to 200B params, fine-tune up to 70B.

Key ratio: ~1 PFLOP FP4-sparse against 273 GB/s is a FLOPS/byte ratio near 1800:1 (FP4-sparse). An H100 is roughly 60:1 in matched precision. The Spark is extremely compute-heavy and bandwidth-starved. Consequence: prefill (compute-bound, parallel over prompt tokens) flies; decode (bandwidth-bound, one full weight-stream per token) crawls.

### Roofline numbers (Q4 weights approx 0.5 byte/param)

Decode ceiling = memory BW / model bytes per stream:

| Model | Bytes (Q4) | Spark 273 GB/s | M3 Max ~300 GB/s | M4 Max / M3 Ultra 400-800 GB/s |
|---|---|---|---|---|
| 7B | ~4 GB | ~68 tok/s | ~75 tok/s | ~100+ tok/s |
| 32B | ~16 GB | ~17 tok/s | ~19 tok/s | ~25-50 tok/s |
| 70B | ~35 GB | ~7.8 tok/s | ~8.6 tok/s | ~11-22 tok/s |
| 120B | ~60 GB | ~4.5 tok/s | OOM (typ. 36-48 GB) | OOM / marginal |

A fast Mac beats Spark per-stream on the same model. Spark never wins decode.

Prefill time approx 2*N*seq_len / FLOPS. 70B, 2k-token prompt at ~250 TFLOPS FP8-effective: ~1.1 s on Spark. A Mac GPU (~30-50 TFLOPS effective) takes ~6-9 s. Spark wins prefill roughly 5-8x.

Capacity: 70B Q4 (35 GB) and 120B Q4 (~60 GB) plus KV fit easily in 128 GB unified. 200B Q4 (~100 GB) fits weights-only per NVIDIA's claim and is KV-constrained. No laptop in the fleet comes close.

### Role ranking

1. (b) Prefill offload node (best). Disaggregated prefill/decode (Splitwise; DistServe) exists precisely because prefill is compute-bound and decode is memory-bound. Spark absorbs prompts, streams KV cache over its 200 GbE to Mac decoders. Its FLOPS advantage is fully used and its bandwidth weakness is bypassed.
2. (a) Big-model solo host (unique). The only node that can hold 70B-120B Q4 with zero pipeline hops. Even at ~8 tok/s it unlocks the stretch tier (Llama-3.3-70B, Qwen2.5-Coder-32B) with no network fragility. No other role adds a capability the pool otherwise lacks.
3. (c) Speculative verifier (strong). Verification scores a draft block in parallel, which is compute-bound like prefill. Macs draft with 0.5B-1.5B models, Spark verifies against the 7B-70B target. Fits the architecture doc's experimental remote drafting section. Keep proposal sizes small and verification target-authoritative.
4. (e) Coordinator plus scheduler host (co-locate, do not dedicate). 20-core Arm, always-on desktop power profile, best NIC in the pool. Good home for the coordinator, but dedicating 1 PFLOP to scheduling wastes it. Run coordinator + prefill worker on the same box.
5. (d) Embedding/batch node (last). It can, but it is wasteful: embedding models are 0.1-1.5 GB and run fine on the weakest laptop. Reserve Spark for work only it can do.

## 2. Heterogeneous scheduling

tcj's architecture doc already prescribes the right primitive: time-limited capacity offers with measured service envelopes (prefill time, decode rate at tested batch sizes), not static hardware ratings.

- Memory-tiered model assignment. Spark tier: 70B-120B solo, prefill pool. Mac tier: 7B replicas, decoders, drafters. Weak/Arch tier: 7B replicas at low concurrency, embedding replicas. Never place by core count. Place by measured prefill-tok/s and decode-tok/s per model.
- Measured envelopes per (node, model, batch). Benchmark each joiner (the arch doc already requires this) and estimate completion as queue + prefill + generation + transfer (Step B formula). A single tok/s score misleads because prefill and decode scale differently per node.
- Straggler rule. In any pipeline group the slowest stage gates every token. Practice from volunteer pipelines and DistServe bandwidth-aware placement: keep pipeline groups speed-homogeneous, size shards proportional to measured throughput (uneven splits, more layers on faster nodes), and prefer Spark solo-hosting big models over pipelining them precisely to dodge the straggler tax. If a pipeline must span uneven nodes, put the slow node early with fewer layers and deepen microbatching so the pipe stays full. Throughput still caps at the slowest stage.
- Literature. Splitwise (Patel et al., arXiv:2311.18677): heterogeneous phase-split clusters give 1.4x throughput at 20 pct lower cost, or 2.35x at equal cost, by matching hardware to phase affinity. DistServe (Zhong et al., arXiv:2401.09670, OSDI'24): co-optimize per-phase parallelism, place phases by cluster bandwidth, 7.4x goodput/SLO result. Speculative decoding (Leviathan et al., arXiv:2211.17192): 2-3x without output changes when verifier acceptance is high. Mélange-class allocators add the tiering argument: match job SLO to cheapest sufficient tier. prima.cpp / exo / HeteGen / Helix specifics could NOT be verified from this environment (web search blocked, one arXiv ID guess resolved to an unrelated paper). Treat those four names as leads to confirm, not citations.
- Concrete tcj policy. Default every laptop to 7B replica. Spark takes prefill overflow + 70B solo + verifier. Offer pipeline shard roles only to strong stable joiners. Keep hysteresis on role switches (arch Step D) to amortize weight loads.

## 3. Embedding workloads as filler

Embarrassingly parallel: one forward pass per text, no autoregression, no KV cache growth, no cross-request state. Batching scales near-linearly until compute-bound. Requests are independent, so any node loss is a cheap retry.

Per-node fits: bge-small (133M, ~0.3 GB Q8), nomic-embed-v1.5 (137M), gte-base (137M), bge-base (109M), gte-large (434M, ~1 GB), bge-m3 (568M, ~1.2 GB). Even a 4 GB weak laptop holds the model plus large batches.

Why ideal filler: tiny weights mean near-zero load time, they soak idle gaps between code tasks, they run on nodes too weak for 7B decode, and they never need pipeline or speculative machinery. Keep a separate embedding replica pool with its own queue so filler never blocks the $0.20 code-task SLO.

## Open items

- Web search engines were blocked in this environment, so GB10 third-party tok/s benchmarks and prima.cpp/HeteGen/Helix/Mélange primary sources still need confirmation.
- Spark's real prefill/decode numbers need a llama.cpp / llama-server benchmark run on arrival.
