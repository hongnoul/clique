# DGX Spark (GB10) — Fresh Ideas From Scratch

Clean-slate brainstorm. Ignores prior research direction. Grounded in what the box actually is:
128GB unified memory, ~273GB/s bandwidth, ~1 PFLOPS FP4, 20 Arm cores, ConnectX-7 200GbE, low power, sits on a desk.

The honest read: **huge memory, huge FP4 compute, mediocre bandwidth**. So the best ideas are ones that are memory-capacity-bound or compute-bound, not decode-bandwidth-bound. Batch > interactive. Always-on > bursty.

---

## Tier 1 — Plays directly to the hardware

### 1. Overnight batch brain
A queue you dump work into all day; Spark burns through it overnight at high batch size (where FP4 compute shines and bandwidth amortizes). Summarize every paper you saved, review every PR, triage email, generate embeddings for everything you touched. Morning digest. Nobody optimizes for latency-insensitive personal batch inference; this box is ideal for it.

### 2. Personal RAG of everything (128GB = the index lives in RAM)
Index your entire life: repos, notes, mail, browser history, screenshots (OCR'd), transcripts. Embeddings + reranker + a 70B answerer all resident simultaneously in unified memory. No tiering, no cold starts. The moat is that a 128GB single box holds embedding model + vector index + LLM + KV cache at once.

### 3. Local agent farm / swarm host
Run 10-30 concurrent coding agents against a shared 70B-class model with prefix-cached system prompts. Batched decode across agents recovers the bandwidth loss. Spark becomes the "agent mainframe" and laptops are thin clients. Pairs naturally with jcode swarm mode.

### 4. Continuous fine-tuning loop (the sleep-learns machine)
Every night: LoRA/QLoRA fine-tune a personal model on the day's accepted diffs, edits, and corrections. 128GB fits a 70B QLoRA run. Wake up to a model slightly more you-shaped. Nobody does continual personal fine-tuning because cloud GPU rental makes the loop annoying; an always-on local box makes it a cron job.

### 5. Speculative-decoding server for the whole LAN
Spark hosts a big verifier (70B) plus several small drafters. Macs on the LAN get an OpenAI-compatible endpoint that is faster than any of them could do alone, because verification is batched/compute-bound (Spark's strength) while drafting is cheap.

## Tier 2 — Strong, more speculative

### 6. Synthetic data foundry
Generate and filter synthetic training data (instruction pairs, unit tests, distillation traces) 24/7 at FP4 batch throughput. Sell nothing; use it to distill small task-specific models (idea 4's fuel).

### 7. Video/multimodal understanding pipeline
VLM batch pass over security cam / screen recordings / lecture videos. Bandwidth doesn't matter, capacity does: long-context video tokens need the 128GB. Output: searchable timeline of your visual life.

### 8. Local eval + regression lab for models
Every new open-weights release, automatically download, quantize, run your personal eval suite (your real prompts, your codebases), publish a scoreboard. "Should I switch models?" answered empirically, continuously.

### 9. World-model / RL gym box
Self-play or environment rollouts (code agents in sandboxes, game envs) where compute is the bottleneck and rollouts are embarrassingly parallel. 20 Arm cores run the envs, GPU scores/policies them.

### 10. Whole-house voice + presence assistant
Always-listening Whisper-class ASR + local LLM + TTS, sub-second, zero cloud. 128GB means ASR, diarization, LLM, TTS all resident. Latency is fine because a single voice stream needs tiny decode throughput.

## Tier 3 — Weird but interesting

### 11. KV-cache warehouse
Precompute and store prefills of things you'll ask about again (your repos, docs, books) as reusable KV caches on disk/RAM. Question answering becomes decode-only. Turns Spark's weakness (prefill wasted repeatedly) into a database problem.

### 12. Genomics / protein / scientific batch node
ESMFold, AlphaFold-class inference, docking screens. FP4/BF16 compute-heavy, batch-friendly, memory-hungry. A desk box that does real science overnight.

### 13. Personal model distillery as a product
Pipeline (ideas 4+6+8 combined): traces in → synthetic data → nightly distill → eval gate → deploy to your laptop as a 3-8B model that runs everywhere. Spark is the factory; the product is small models.

### 14. LAN game/NPC server
Persistent NPCs with memory for a game or sim, dozens of characters sharing one batched model. Latency tolerant, batch-friendly, always-on.

---

## If forced to pick one
**#3 (agent farm) + #4 (nightly fine-tune) + #11 (KV warehouse) compose into one coherent system:** an always-on personal AI mainframe that serves your agents by day and improves itself by night. Each piece is independently useful and plays to capacity + batch compute, away from the bandwidth weakness.
