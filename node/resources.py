"""Resource probing: produce honest ResourceSnapshots.

psutil for CPU/memory/battery/load. GPU is per-platform:
- NVIDIA: nvidia-ml-py (NVML) for VRAM and utilization.
- Apple Silicon: unified memory reported via psutil; GPU utilization is
  best-effort (powermetrics needs sudo, so default to None).

Rule (ARCHITECTURE.MD §2): missing telemetry -> None, and consumers must
route conservatively. Never fabricate.
"""

from __future__ import annotations

from common.types import ResourceSnapshot


def probe() -> ResourceSnapshot:
    """Take a point-in-time snapshot of this machine's resources."""
    ...


async def benchmark_model(model_endpoint: str, sample_prompt_tokens: int = 512) -> tuple[float, float]:
    """Measure (prefill_tok_s, decode_tok_s) against the locally running
    model server with a canned prompt.

    Run once after model warmup and again if sustained throughput drifts
    >30% from the recorded figure. These measured numbers, not spec
    sheets, feed the router's estimates.
    """
    ...


def recommend_max_concurrency(snapshot: ResourceSnapshot, kv_per_stream_mb: int) -> int:
    """Given available memory and per-stream KV cost, return how many
    concurrent streams this node should advertise (currently 1, since the
    router enforces one task per node, but computed for the future)."""
    ...


def detect_recommended_model(snapshot: ResourceSnapshot) -> str | None:
    """Future feature (vision dump): suggest the best default model this
    hardware can run, e.g. by matching available memory against known
    GGUF sizes from huggingface_hub. Returns a model ref or None to fall
    back to the clique default (Qwen 2.5)."""
    ...
