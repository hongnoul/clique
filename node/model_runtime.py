"""Adapter over the local model runtime.

Primary backend: llama.cpp ``llama-server`` (Metal on macOS, CUDA on
Linux/NVIDIA), spawned and supervised by this module, speaking its
OpenAI-compatible HTTP API on localhost. Secondary backend: Ollama, for
users who already have it (vision dump: "or can connect your own").

Model files come from huggingface_hub with revision pinning; the server
node can also proxy GGUFs over LAN so joiners avoid WAN pulls
(ARCHITECTURE.MD §1a).
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from common.types import ModelSpec


class ModelRuntime:
    """Owns exactly one loaded model process/connection per node."""

    def __init__(self, backend: str, model_dir: Path) -> None:
        """backend: "llama-server" | "ollama"."""
        ...

    async def ensure_model(self, model_ref: str, lan_mirror: str | None = None) -> ModelSpec:
        """Resolve model_ref (HF repo id or cluster key) to a local GGUF,
        downloading from lan_mirror first, then Hugging Face. Verifies
        sha256. Returns the concrete ModelSpec (with artifact_hash)."""
        ...

    async def load(self, spec: ModelSpec) -> None:
        """Start/attach the backend with the model, wait for warmup
        (first inference completes), raise ModelNotReadyError on failure."""
        ...

    async def unload(self) -> None:
        """Stop the backend process and verify memory is actually
        reclaimed (RSS/VRAM drop), not just that the process exited."""
        ...

    async def infer_stream(
        self,
        prompt: str,
        context_blob: bytes | None,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Run one generation, yielding text deltas. context_blob is the
        serialized session context to prepend (server-held sessions)."""
        ...

    async def cancel(self) -> None:
        """Abort the in-flight generation promptly (MsgRevokeTask path)."""
        ...

    async def health(self) -> bool:
        """True only if the backend answers a real (tiny) inference, not
        just a TCP connect: reachable-but-busy is not healthy capacity
        (ARCHITECTURE.MD §2)."""
        ...
