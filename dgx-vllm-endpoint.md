# GX10 high-throughput inference endpoint (2026-09-19)

## Endpoint
- OpenAI-compatible: `http://100.83.233.124:8000/v1` (tailscale) / `http://127.0.0.1:8000/v1` (on gx10)
- Model name: `nemotron-3-nano-fp8` (NVIDIA-Nemotron-3-Nano-30B-A3B-FP8, ModelOpt FP8)
- Ollama untouched on `:11434`.

## Measured TPS (256-tok generations)
| config | TPS |
|---|---|
| Ollama nemotron-3-nano:30b Q4_K_M (baseline) | 80.5 single |
| Ollama gpt-oss:20b (baseline) | 63.9 single |
| vLLM FP8 single stream | 46-47 |
| vLLM 8 concurrent | 167 aggregate |
| vLLM 16 concurrent | 215 aggregate |
| vLLM 32 concurrent | **340-347 aggregate** |

## Serve command (running on gx10, restart unless-stopped)
```bash
docker run -d --name vllm --gpus all --ipc=host --ulimit memlock=-1 --ulimit stack=67108864 \
  --restart unless-stopped -p 8000:8000 \
  -e VLLM_USE_FLASHINFER_MOE_FP8=1 -e VLLM_FLASHINFER_MOE_BACKEND=throughput \
  -v /home/asus/.cache/huggingface:/root/.cache/huggingface \
  -v /home/asus/.cache/vllm:/root/.cache/vllm \
  nvcr.io/nvidia/vllm:25.12.post1-py3 \
  vllm serve /root/.cache/huggingface/hf-full \
    --served-model-name nemotron-3-nano-fp8 --trust-remote-code --async-scheduling \
    --kv-cache-dtype fp8 --max-model-len 32768 --max-num-seqs 32 \
    --gpu-memory-utilization 0.80 --enable-prefix-caching
```

## Clique join
```bash
clique join --runtime openai-compat --base-url http://127.0.0.1:8000/v1 \
  --model-name nemotron-3-nano-fp8 --param-b 30
```

Notes:
- FlashInfer CUTLASS FP8 MoE kernels verified active in logs. GB10 (sm_121) has no native FP4 compute path, so FP8 + CUTLASS is the fast path on this silicon; NVFP4 checkpoints only save bandwidth, they do not add compute speed here.
- Single-stream TPS is bandwidth-bound (~46); throughput comes from vLLM continuous batching, which Ollama does not do (Ollama 8-way = same as 1-way).
- Cold start after reboot ~8-10 min (weight load + torch.compile warm).

## Speculative decoding: dead end for this model (tested 2026-09-19)
- Checkpoint has no MTP weights (`model.safetensors.index.json` has zero mtp keys).
- ngram spec decoding crashes: `NotImplementedError: Mamba with speculative decoding is not supported yet` (vLLM 0.12). Nemotron-3-Nano is a Mamba-hybrid, so all spec decoding is off the table until vLLM adds Mamba support.
- Warm single-stream is 44-46 TPS, hard bandwidth-bound. Clique should batch tasks (fan out subtasks) to exploit the 340+ aggregate.

## End-to-end clique verification
- clique-server on gx10 :7777, node `gx10-vllm` joined with openai-compat -> vLLM :8000.
- `clique submit "Write a haiku about fast GPUs"` routed, streamed, and committed in 8.3s.

## Boot durability (2026-09-19)
- `vllm` docker container: `--restart unless-stopped`.
- `clique-server.service` and `clique-node.service` installed and enabled on gx10 (systemd). Node unit waits for vLLM readiness before joining, so the full stack self-assembles after reboot.
- Verified: systemd-managed stack served a clique submit end-to-end (0.6s).

## Parallel slots (2026-09-19): clique now exploits vLLM batching
Closing the loop end-to-end exposed that the router's one-task-per-node policy
serialized everything: 8 concurrent clique submits took 79.7s even though raw
vLLM did the same work in ~12s. Added `parallel_slots` (ModelSpec/config/CLI
`--parallel-slots`) with slot-aware scheduling in the router and a multi-task
agent. gx10 node runs with 16 slots.

Measured through the full clique pipeline (submit -> route -> vLLM -> stream -> commit):
- 8 concurrent submits: 79.7s -> 27.9s (ok 8/8)
- 16 concurrent submits: 53.1s, ok 16/16 (single node)

## Memory rule (learned the hard way, 2026-09-19)
GB10 unified memory means vLLM's reservation, Ollama's model loads, and the OS
all share one 121GB pool. At --gpu-memory-utilization 0.80 (~97GB) there is NO
room for a second model: routing a task to an Ollama-backed node loaded qwen35b
(+22GB) and hard-locked the box (needed a power cycle).

Policy: gx10 is single-model, all-in on Nemotron via vLLM at 0.80.
- ollama.service is disabled (binary kept; enable manually if ever needed,
  but only after dropping vLLM to <=0.60).
- clique-node-qwen unit removed.
- Post-restore bench: single 46.8, 8-way 167, 16-way 238, 32-way 355 agg TPS.
- Real cold-boot verified: after power cycle the docker container and systemd
  units self-assembled; one unit fix was needed because the gx10 tree synced to
  merged main (no --active-param-b flag anymore).
