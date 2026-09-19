# Parallelism Cross-Sections for tcj: Viability on Heterogeneous Consumer Nodes

Date: 2026-09-19. Fleet: volunteer laptops (Macs w/ Metal, Linux w/ CUDA) + 1 NVIDIA DGX Spark, coordinated by one scheduler, over LAN / Tailscale / WAN. Demand: batch code tasks, delay-tolerant, verified with tests.

## TLDR

Replica-first is correct and is the money loop. Pipeline parallelism (llama.cpp RPC) is the only viable cooperative mode on 1GbE LAN and usable-over-Tailscale for batch jobs. Tensor parallelism is dead on any consumer network except a Thunderbolt-RDMA Mac island (exo). MoE needs no special treatment if experts stay colocated with their layers. Cross-node speculative decoding and disaggregated prefill/decode are network-cheap in theory but unsupported by any consumer-grade runtime; run speculation locally per replica instead, and skip disaggregation.

## 0. Baseline numbers used throughout

Per-token pipeline hop payload = one activation vector = hidden_dim x 2 bytes (fp16):

| Model | hidden_dim | Bytes per token per hop |
|---|---|---|
| Qwen2.5-Coder-7B | 3584 | ~7 KB |
| Qwen2.5-Coder-32B | 5120 | ~10 KB |
| Llama-3.3-70B | 8192 | ~16 KB |

At 10 tok/s, even the 70B hop is ~160 KB/s (~1.3 Mbps). Pipeline is latency-bound, not bandwidth-bound.

Tensor-parallel cost per token (dense 70B, 80 layers): 2 all-reduces per layer, each moving roughly hidden_dim x 2B per rank, so ~2.6 MB of collective traffic per token, spread over ~160 network round-trips on the critical path. At 10 tok/s that is ~200-400 Mbps sustained plus latency accumulation. This is why TP needs datacenter fabric.

Disaggregation handoff cost: KV cache = 2 x layers x kv_heads x head_dim x seq_len x bytes_per_elem. Qwen2.5-7B (28 layers, 4 KV heads, head_dim 128, fp16): ~28-64 KB per context token, so a 4k-token prompt hands off ~110-256 MB per request. On 1GbE that is ~1-2 s added to TTFT, erasing the prefill win.

## 1. Replica / data parallelism (whole model per node): baseline

Each laptop holds a whole Qwen2.5-Coder-7B Q4 (~4.7 GB weights + 2-4 GB KV for 8k context). Zero per-token network traffic; only job dispatch and result return cross the wire. Throughput scales near-linearly with node count. Single-request speed equals that one laptop (Mac ~40-60 tok/s single stream, Arch/CUDA ~15-25 tok/s per vision.md projections). Failure blast radius is one job: retry on another node, no duplicate commit (fenced attempt IDs per architecture doc).

Verdict: viable on every network including WiFi and WAN. This is the HackMIT money loop and needs no further justification.

## 2. Pipeline parallelism (layer sharding)

Runtimes: llama.cpp RPC backend (primary recommendation), exo (heterogeneous auto-discovery), prima.cpp, Petals (public swarm, research only).

How llama.cpp RPC works: build every machine with GGML_RPC=ON at the identical pinned tag, run rpc-server on workers, point llama-server --rpc at a comma-separated worker list with -ngl 99, weight uneven machines with --tensor-split. Gotchas from the manual: version skew hangs at load (most common failure); some quantized shapes assert-fail over RPC (row count not multiple of 512); slowest node pins the chain; rpc-server has NO authentication so it must live inside Tailscale/WireGuard; local disk cache (-c) speeds reloads. New: llama.cpp RPC now negotiates an RDMA transport (RoCEv2 on Linux via libibverbs, RDMA-over-Thunderbolt on Apple Silicon Macs on macOS 26.2+ via librdma, enabled once from Recovery with rdma_ctl enable), falling back to TCP otherwise.

Measured 70B Q4 2-node pipeline throughput: 1GbE 2.8 tok/s (workable for batch, painful for chat), 2.5GbE 6.1 tok/s (home-lab sweet spot), 10GbE 7.4 tok/s, Thunderbolt 4 7.6 tok/s (diminishing returns past 2.5GbE). TTFT penalty is +50-200 ms minimum; 4k-token prefill across 2 nodes takes ~4-6 s vs ~1.5 s single-box. llama-server RPC has no continuous batching (single-stream plus queue), so microbatching gains come from running concurrent jobs across replica groups, not within one pipeline. exo adds topology-aware auto-parallel placement and now ships day-0 RDMA-over-Thunderbolt-5 support (claimed 99% latency cut) plus MLX backend, but Linux workers are currently CPU-only, so exo is Mac-centric for tcj today. WiFi collapses throughput up to 10x (SharedLLM field report); Tailscale adds ~1-5 ms per hop, acceptable for batch.

Bandwidth/latency per mode: 7B ~7 KB, 32B ~10 KB, 70B ~16 KB per token per hop; latency added = hops x RTT (1GbE ~0.3-0.6 ms one-way, WiFi 5-15 ms jitter, Tailscale +1-5 ms). Failure blast radius: the whole group job stalls on any shard loss; recovery = restart worker, reload model on primary; caches and in-flight requests are lost, so keep pipeline jobs retryable and short.

## 3. Tensor parallelism: why it fails on consumer networks

Every matmul in every layer issues an all-reduce on the critical path: ~160 network round-trips per token for an 80-layer model moving ~2.6 MB per token at 70B. Measured cross-node vLLM TP=2 on 2.5GbE: 1.8 tok/s, versus 6.1 tok/s for pipeline on the same wire and 14.2 tok/s single-box NVLink. Rule: TP needs 25GbE+ to be acceptable, 100GbE/NVLink in datacenters. Additional blockers: vLLM multi-node requires homogeneous GPUs plus Ray, config is unforgiving, and 1GbE saturates instantly.

Exceptions: (a) Thunderbolt-RDMA Mac island via exo, which supports TP sharding and claims 1.8x on 2 devices / 3.2x on 4 (cf. Jeff Geerling 4x M3 Ultra Mac Studio DeepSeek-671B runs); (b) multiple GPUs inside ONE box (NVLink/PCIe), which is single-node TP, not the network question. For tcj: TP viable only on a Thunderbolt-RDMA Mac island, never on WiFi / 1GbE / Tailscale / WAN.

## 4. Expert / MoE parallelism

Two placements. (a) Experts colocated with their layers inside pipeline shards: zero extra communication, behaves exactly like the pipeline case. This is what llama.cpp RPC does (it has no EP support; layers just split, experts ride along). (b) Experts sharded across nodes: all-to-all dispatch per token per MoE layer (Qwen3-30B-A3B has 48 MoE layers, 128 experts, top-8 routing) = strictly worse than TP. vLLM documents Expert Parallel Deployment with EPLB, but it assumes datacenter fabric.

Verdict: always choose (a) on consumer networks; the tcj architecture doc already states this correctly. Qwen3-30B-A3B MoE Q4 (~19 GB) fits 2-3 laptops via plain pipeline with experts intact. No runtime work needed beyond pipeline.

## 5. Speculative decoding across nodes (draft on small node, verify on big node)

Network cost is trivially cheap: draft token IDs one way (tens of bytes for gamma=5), accept/reject plus logits back (~300 KB worst case per step at vocab 152k fp16, or a few bytes under greedy). The blocker is interfaces, not bandwidth. Required: shared tokenizer, a draft-distribution or token-sequence API, and target-side verify-with-rollback (KV-cache rewind on reject). Ordinary chat/completions endpoints expose none of this.

Runtime reality: llama.cpp supports only local --draft-model. vLLM supports draft-model, EAGLE, MTP, n-gram, suffix, and an experimental custom-proposer backend, but all colocated in one engine, and pipeline parallelism is explicitly NOT composable with speculative decoding as of v0.15. A remote drafter behind the custom proposer is DIY glue with untrusted-proposal risk (cap proposal sizes, target-authoritative verification). Practical verdict: skip cross-node speculation for the weekend; instead run LOCAL speculation per replica (llama-server --draft-model with a small Qwen draft, or vLLM n-gram/suffix with zero extra model) to lift single-stream rates. Remote draft/verify is a post-weekend experiment.

## 6. Disaggregated prefill / decode (prefill on DGX Spark, decode elsewhere)

KV transfer per handoff is ~110-256 MB for a 4k prompt on a 7B model (Section 0 math), i.e. ~1-2 s on 1GbE added to TTFT, erasing the fast-prefill win. DistServe (OSDI 2024, Zhong et al.) reports 7.4x goodput or 12.6x tighter SLOs, but assumes datacenter bandwidth and co-optimized per-phase parallelism. vLLM has disaggregated-serving support (Mooncake/NIXL KV transfer) aimed at datacenter NICs. No consumer-grade runtime (llama.cpp, exo, ollama) supports split-phase serving.

Verdict: not viable for tcj on 1GbE/WiFi/Tailscale. Revisitable only as a DGX-Spark-prefill plus Thunderbolt/10GbE decode island later. Note the DGX Spark (128 GB unified memory) holds a 70B Q4 whole (~40 GB), so it is more valuable as a 70B replica or pipeline primary than as a prefill node.

## 7. Verdict table (batch code tasks)

| Mode | WiFi | LAN 1GbE | 10GbE | Thunderbolt | Tailscale/WAN | Per-token network |
|---|---|---|---|---|---|---|
| Replica / DP | OK-marginal | YES | YES | YES | YES | 0 (dispatch only) |
| Pipeline (llama.cpp RPC) | MARGINAL (jitter, 10x risk) | YES-batch | YES | YES (+RDMA) | YES-batch-only | ~7-16 KB/hop, latency-bound |
| Tensor parallel | NO | NO | MARGINAL | YES iff Mac RDMA island | NO | ~2.6 MB collective, 160 RTTs (70B) |
| MoE experts-with-layers | = pipeline | = pipeline | = pipeline | = pipeline | = pipeline | = pipeline |
| MoE sharded experts | NO | NO | NO | NO | NO | all-to-all, worse than TP |
| Remote speculative draft/verify | NO (no runtime) | NO (no runtime) | NO (no runtime) | DIY only | NO | ~bytes-KB/step, cheap but unsupported |
| Local speculative (per replica) | YES | YES | YES | YES | YES | 0 (local) |
| Disaggregated prefill/decode | NO | NO (~256 MB handoff) | MARGINAL | POSSIBLE | NO | ~110-256 MB per 4k request |

## 8. Recommendation for tcj

Ship replica-only 7B money loop for the demo. Stretch path: llama.cpp RPC pipeline for 30B-MoE / 32B / 70B on wired LAN or Tailscale, with the DGX Spark as pipeline primary or whole-70B replica. Add local per-replica speculative decoding as the cheap speedup. Defer tensor parallelism (except a future Thunderbolt Mac island), remote speculation, and disaggregation to post-weekend experiments.

## Sources

- llama.cpp RPC backend README (ggml-org/llama.cpp, tools/rpc): TCP/RPC device model, --rpc/--tensor-split usage, RDMA transport (RoCEv2, Thunderbolt), no-auth warning.
- SharedLLM blog: llama.cpp RPC distributed inference manual (setup, version-skew hangs, Q4_K 512-alignment asserts, 10x WiFi collapse, slowest-node pins chain).
- LocalAimaster: Distributed Inference for Local AI (2026) — TP vs PP vs DP decision tree, 70B Q4 tables (1GbE 2.8 / 2.5GbE 6.1 / 10GbE 7.4 / TB4 7.6 tok/s pipeline; vLLM cross-node TP 1.8 vs 14.2 single-box NVLink).
- exo GitHub (exo-explore/exo, 47.5k stars): auto-discovery, topology-aware auto-parallel, MLX backend, day-0 TB5 RDMA, Linux CPU-only caveat.
- vLLM docs: speculative decoding methods (draft/EAGLE/MTP/ngram/suffix, custom proposer experimental) and PP-not-composable-with-spec-decode as of v0.15; Expert Parallel Deployment; disaggregated serving (Mooncake/NIXL).
- DistServe (Zhong et al., OSDI 2024, arXiv:2401.09670): prefill/decode disaggregation gains under datacenter assumptions.
- NCCL docs (NVIDIA) for TP collective background.
