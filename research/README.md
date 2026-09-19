# Research (single source of truth)

All tcj research lives here. Do not create `docs/research/` or other research folders.

| File | What it is |
|---|---|
| `parallelism-modes.md` | Detailed parallelism cross-sections: replica, pipeline, tensor, MoE, speculative, disaggregated prefill/decode. Evidence and numbers. |
| `red-fable.md` | Swarm synthesis (4 workers) with verdict matrix and 3-tier recommended architecture. Start here for conclusions. |
| `frameworks-survey.md` | Distributed inference frameworks survey (llama.cpp RPC, exo, prima.cpp, Petals, distributed-llama, vLLM/SGLang, GPUStack). Fit ranking for tcj. |
| `dgx-spark-hetero-scheduling.md` | DGX Spark role analysis + heterogeneous scheduling policy + embedding filler. |

Overlap note: `red-fable.md` is the conclusions layer over `parallelism-modes.md` evidence. If they disagree, `red-fable.md` wins on architecture and `parallelism-modes.md` wins on raw numbers.
