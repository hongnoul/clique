# Distributed Inference Frameworks Survey for tcj

Date: 2026-09-19. Context: tcj is an idle-compute network serving LLM batch inference on volunteer consumer laptops (Macs with Metal, Linux with CUDA) plus one NVIDIA DGX Spark. Primary path is replica groups (one whole Qwen2.5-Coder-7B Q4 GGUF per laptop behind an OpenAI-compatible coordinator). Stretch path is pipeline sharding for 30B/70B class models. Nodes churn; specs are heterogeneous within variance.

## 1. llama.cpp RPC mode (rpc-server + llama-server)

- Repo: ggml-org/llama.cpp (~129k stars), MIT, actively maintained.
- How it works: build every node with `-DGGML_RPC=ON` pinned to the SAME tag. Run `ggml-rpc-server` on each worker. Run `llama-server --rpc host1,host2,...` on the primary. The primary shards model layers across local + remote devices, weighted by `--tensor-split`.
- Platforms: mixed Metal + CUDA + CPU confirmed. Official README topology diagram shows CUDA, Metal, and CPU workers. The sharedllm.org tutorial (2026-07-18) covers any mix of Metal/CUDA/CPU and more than two workers.
- Parallelism: layer sharding across RPC devices (memory pooling, not speedup). New RDMA-over-Thunderbolt transport (macOS 26.2+, Thunderbolt 5) with TCP fallback.
- Quantization: native GGUF including Q4_K_M. Caveat: some Q4_K/Q5_K shapes assert-fail on serialization when a row count is not a multiple of 512; workaround is Q8_0 or a 512-aligned hidden dim. Llama, Qwen, Phi-3 are fine.
- API: llama-server is OpenAI-compatible.
- Fault tolerance: none. A dead worker means reloading the model on the primary; in-flight tokens die. No retry or resume.
- Operational quirks: README labels it proof-of-concept, fragile and insecure. rpc-server has NO authentication, so bind to Tailscale or WireGuard only, never the open internet. Version skew across nodes hangs silently at load or handshake. Default layer split is even, which is wrong for uneven nodes and must be hand-tuned with `--tensor-split`. Slowest hop sets token speed. WiFi costs roughly 10x vs wired Ethernet or Thunderbolt bridge. Local tensor cache (`-c`) speeds model load.
- Verdict: KEEP as the tcj stretch path for Qwen3-30B-A3B, 32B, and 70B on a stable 2 to 4 node wired subset. Never the churn-tolerant primary.

## 2. exo (exo-explore/exo)

- Repo: exo-explore/exo (47.5k stars), Apache-2.0. Active: latest commit 2026-08-25 (docs fix for mlx extras). Recent work includes model cards such as Kimi K2.7-Code with distributed serving tested on 2x Mac Studio M3 Ultra.
- Features: automatic device discovery over libp2p (no manual IPs), topology-aware auto parallel (pipeline + tensor parallel via MLX distributed, JACCL/ring backends), day-0 RDMA over Thunderbolt 5, OpenAI/Claude/Ollama-compatible APIs, built-in dashboard, custom HuggingFace model loading, benchmarks up to 235B-671B on 4x M3 Ultra Mac Studios.
- Platforms: Mac-first. The README states that on Linux, exo currently runs on CPU and GPU support is under development. So CUDA laptops and the DGX Spark get no GPU acceleration today.
- Quantization: MLX/HF safetensors quants, not GGUF. This breaks tcj's Qwen Q4 GGUF plan.
- Fault tolerance: placement is static per instance; no churn or resume story for volunteer laptops that sleep or leave mid-job.
- Verdict: best option if the fleet were Mac-only. As specified, REJECT as fabric. Borrow discovery and topology-aware placement ideas.

## 3. prima.cpp (OpenCPIL/prima.cpp)

- Repo: OpenCPIL/prima.cpp, MIT, ICLR 2026 paper ("Prima.cpp: Fast 30-70B LLM Inference on Heterogeneous and Low-Resource Home Clusters"). Tiny: 99 stars, 16 forks, 9 commits, pushed 2026-09-06. Alive but a research prototype.
- Ideas (most relevant scheduler work surveyed): heterogeneity-aware LP scheduler (HiGHS) assigning layers by compute, disk speed, memory, and OS; piped-ring parallelism with mmap lazy-load and prefetch to overlap disk latency; automatic device selection that drops weak nodes; GGUF Q4K/Q6K/Q80/IQ1; speculative decoding (up to 80 percent claimed); server mode with OpenAI chat-completions plus continuous batching (`-np 4 --cont-batching`); mix of macOS, Linux, Android/Termux.
- Caveats: FAQ admits CUDA-only GPUs (Mac joins as CPU; docs even suggest `LLAMA_NO_METAL=1` for very large models). Static `--world/--rank` ring: a leaving node breaks the ring with no retry or resume. Benchmark tables vs llama.cpp/exo/distributed-llama are author-run and unverified. Windows unsupported.
- Verdict: BORROW the profiling, scheduling, prefetch, and device-selection ideas. Do not deploy for a churning fleet.

## 4. Petals (bigscience-workshop/petals)

- Repo: bigscience-workshop/petals (10.6k stars), MIT. STALE: last commit 2024-08-25, roughly 2 years dead.
- Design: BitTorrent-style volunteer swarm. Each volunteer serves a slice of transformer blocks; DHT routing; clients pipeline tokens through other people's servers. Supports Llama 3.1 405B, Mixtral, Falcon in torch/HF fp16/8-bit.
- Why latency is high: every generated token sequentially traverses the full chain of remote strangers over the open internet (WAN round trip per hop per token), single-batch pipeline with no batching gain, and stragglers or retransmits stall the whole chain. The paper's own numbers are about 4 to 6 tok/s on 70B to 180B class models.
- Other blockers: no GGUF, no Metal-first story, no OpenAI batch API, trust and privacy exposure on the public swarm (workers see prompts and outputs).
- Verdict: REJECT. Closest to tcj's volunteer idea in spirit, furthest in latency and maintenance.

## 5. distributed-llama (b4rtaz/distributed-llama)

- Repo: b4rtaz/distributed-llama (3.1k stars), MIT. Low activity: last commit 2026-07-05.
- Design: tensor parallelism over plain TCP Ethernet with a root/worker topology. The root loads the model and forwards slices, then synchronizes state. Runs on Linux, macOS, Windows; ARM and x86; CPU plus experimental Vulkan GPU. Supports Llama and Qwen including MoE, with 70B-on-4x-Mac-Mini and 405B demos.
- Hard limits: node count must be a power of 2 and at most the model's KV head count. Only q40/f32 in its own converted format (converter required, no GGUF, no Q4_K_M). The root is a single point of failure. No auth, no churn handling, no real batching. `dllama-api` exists but is not OpenAI drop-in.
- Verdict: REJECT for tcj. Usable at most as a Mac-CPU demo curiosity for 70B.

## 6. vLLM + Ray, SGLang

- Repos: vllm-project/vllm and sgl-project/sglang, both Apache-2.0, very active datacenter standards.
- Strengths: multi-node via Ray/NCCL with tensor, pipeline, data, and expert parallel; continuous batching; paged attention and KV-cache reuse; disaggregated prefill/decode supported by both. Most mature serving code surveyed.
- Mac story: vLLM docs say macOS Apple Silicon is experimental CPU-only build-from-source with no prebuilt wheels (FP32/FP16 only); the only GPU Metal path is the community vllm-metal/MLX plugin. SGLang requires Linux NVIDIA/AMD. Neither reads GGUF. Both assume a stable low-latency fabric, not volunteer WiFi churn.
- Verdict: deploy vLLM or SGLang ONLY as a single-node fast engine on the DGX Spark (Linux CUDA). They cannot incorporate the Macs.

## 7. GPUStack, Kalavai, others

- GPUStack (gpustack/gpustack, 5.7k stars, Apache-2.0, very active): full cluster manager with OpenAI-compatible gateway, vLLM/SGLang orchestration, scheduler, auth, metering, and Grafana/Prometheus. But workers are Linux-only; macOS is explicitly unsupported as a worker node. Worth borrowing for ops patterns and possibly managing a Linux/CUDA subfleet including the Spark.
- Kalavai (kalavai-net/kalavai-client, 221 stars, Apache-2.0, pushed 2026-09-15, active): spare-GPU aggregation over Kubernetes, Linux/NVIDIA-oriented, small community, no Mac story.
- SharedLLM (MHASK/sharedllm, AGPL-3.0): a coordinator over raw llama.cpp RPC with discovery and an HMAC-authenticated proxy. Notable as prior art for exactly tcj's stretch-path wrapper, but AGPL licensing and added dependency argue for building tcj's own thin coordinator instead.
- Verdict: REJECT as tcj fabric. GPUStack is ops inspiration and a possible Linux-subfleet manager.

## Fit ranking for tcj (OpenAI-compatible batch, churn, hetero within variance)

1. llama-server-per-replica + tcj coordinator (already planned): the only option meeting Metal plus CUDA plus GGUF plus OpenAI today. Churn handled by retry on another replica.
2. llama.cpp RPC: stretch pipeline for models exceeding one laptop, on a stable subset only.
3. exo: watch. Adopt only if the fleet goes Mac-only or Linux GPU support lands.
4. prima.cpp: idea mine (heterogeneity-aware scheduling, prefetch, device selection). Not deployable.
5. GPUStack: ops inspiration and possible Linux-subfleet manager.
6. distributed-llama: Mac-CPU demo fallback only.
7. vLLM/SGLang: Spark-local engine only.
8. Kalavai: weak fit, skip.
9. Petals: reject (dead, slow, wrong format).

## Citations and status checks (2026-09-19)

- llama.cpp `tools/rpc/README.md` (ggml-org): mixed CUDA/Metal/CPU diagram, `--rpc` usage, `--tensor-split` override, local cache, RDMA transport, no-auth warning.
- sharedllm.org blog "llama.cpp RPC backend: distributed inference across multiple machines" (2026-07-18): same-version pinning, `--tensor-split` weighting, 512-alignment assert, WiFi penalty, no-auth warning.
- exo GitHub repo (exo-explore/exo): 47.5k stars, Apache-2.0, auto discovery, MLX backends, OpenAI/Claude/Ollama APIs. Commits API: latest 2026-08-25. README: Linux currently CPU-only.
- prima.cpp GitHub repo (OpenCPIL/prima.cpp): ICLR 2026, MIT, 99 stars, pushed 2026-09-06. README: HiGHS scheduler, piped-ring, GGUF quants, CUDA-only GPUs, Termux support, no Windows.
- Petals GitHub repo (bigscience-workshop/petals): 10.6k stars, MIT. Commits API: last commit 2024-08-25. README: BitTorrent-style swarm, 4 to 6 tok/s figures.
- distributed-llama GitHub repo (b4rtaz/distributed-llama): 3.1k stars, MIT. Commits API: last 2026-07-05. README: root/worker topology, 2^n node limit, q40/f32-only limit.
- vLLM CPU install docs (docs.vllm.ai): macOS Apple Silicon experimental CPU-only, build from source, FP32/FP16; community vllm-metal plugin for Metal.
- GPUStack GitHub repo (gpustack/gpustack): 5.7k stars, Apache-2.0. README: Linux-only worker nodes, macOS unsupported as worker.
- kalavai-client GitHub API (kalavai-net/kalavai-client): 221 stars, Apache-2.0, pushed 2026-09-15.

Note: web search engines were rate-limited during research, so verification was done via direct fetches of repos, READMEs, docs pages, and the GitHub API.
