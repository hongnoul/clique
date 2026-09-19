# Fleet benchmarks (measured, not projected)

First real datapoints for the benchmark sequence vision.md requires. Reproduce with `llama-bench` and `llama-batched-bench` on each node at join time.

## Node 1: coordinator MacBook (Apple M5 Pro, 48 GB)

Date: 2026-09-19. llama.cpp build b29c606e2 (brew 0.4.1), backend BLAS+Metal.
Model: Qwen2.5-Coder-7B-Instruct Q4_K_M (4.36 GiB), the hackathon primary.

| Test | Measured | vision.md projection | Verdict |
|---|---|---|---|
| Prefill pp512 | 1573.5 tok/s | (not projected) | - |
| Prefill pp2048 | 1460.0 tok/s | (not projected) | - |
| Decode single stream (tg128) | 59.2 tok/s | Mac 40-60 tok/s | PASS, top of range |
| Decode 4 concurrent streams | 139.7 tok/s aggregate, ~34.9/stream | Mac ~25 tok/s per stream loaded | PASS, exceeds by ~40% |

Commands:

```bash
llama-bench -m qwen2.5-coder-7b-instruct-q4_k_m-00001-of-00002.gguf -p 512,2048 -n 128
llama-batched-bench -m <same> -c 8192 -npp 512 -ntg 128 -npl 1,4
```

Implications confirmed against research/:

- The M5 Pro sits in the Pro/Max tier of the red-fable caveat, and its 59 tok/s single-stream validates the 40-60 band for that tier (base M-series will still need their own measurement).
- Batching gain is real on Apple Silicon: 4 streams give 2.36x aggregate decode (139.7 vs 59.2), consistent with the "1.7-2x aggregate" replica-tier claim, slightly better here.
- Prefill at ~1.5k tok/s means a 2k prompt costs ~1.4 s locally, so no prefill offload is needed for 7B-class replicas.
- One node already delivers half the 95 tok/s loaded pool projection, so the 5-laptop target has margin if teammate Macs are Pro-tier.

## To measure next

- Teammate Macs (per-chip decode envelopes, especially any base M-series).
- Arch/CUDA laptops (projected 15-25 tok/s single stream).
- DGX Spark on arrival: prefill and decode envelopes per research/dgx-spark-hetero-scheduling.md open items, plus a 70B Q4 solo-host run.
- llama.cpp RPC 2-node pipeline over LAN for the 30B-MoE stretch (research/parallelism-modes.md section 2 numbers to verify).
