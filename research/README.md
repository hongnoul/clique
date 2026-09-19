# Research (single source of truth)

All tcj research lives here. Do not create `docs/research/` or other research folders.

| File | What it is |
|---|---|
| `parallelism-modes.md` | Detailed parallelism cross-sections: replica, pipeline, tensor, MoE, speculative, disaggregated prefill/decode. Evidence and numbers. |
| `architecture-recommendation.md` | Final synthesis: cross-section verdicts + tiered architecture for the heterogeneous fleet incl. DGX Spark. Start here. |
| `red-fable.md` | Earlier swarm synthesis (4 workers) with verdict matrix and 3-tier recommendation. |
| `red-sol.md` | Independent Apex research synthesis. Converges on the same verdicts (replica-first, "heterogeneous globally, homogeneous locally", no cross-node TP/EP/disaggregation). |
| `frameworks-survey.md` | Distributed inference frameworks survey (llama.cpp RPC, exo, prima.cpp, Petals, distributed-llama, vLLM/SGLang, GPUStack). Fit ranking for tcj. |
| `dgx-spark-hetero-scheduling.md` | DGX Spark role analysis + heterogeneous scheduling policy + embedding filler. |

Overlap note: `architecture-recommendation.md` is the conclusions layer and wins on architecture if any doc disagrees. `parallelism-modes.md` wins on raw numbers. `red-fable.md` and `red-sol.md` are earlier/independent syntheses kept for provenance.
