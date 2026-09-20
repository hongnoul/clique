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
