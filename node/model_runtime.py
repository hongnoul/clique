"""Model runtime adapters (MVP implementation).

Backends:
- EchoRuntime: deterministic fake for tests/demos with no model installed.
- OpenAICompatRuntime: any OpenAI-compatible local server (ollama,
  llama-server, LM Studio) via streaming /chat/completions.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Sequence

import httpx

from common.errors import ModelNotReadyError
from common.types import ChatMessage, ModelSpec, TaskType


def _as_openai_messages(
        prompt: str, messages: Sequence[ChatMessage | dict] | None) -> list[dict]:
    if messages:
        out = []
        for m in messages:
            if isinstance(m, dict):
                out.append({"role": m.get("role", "user"),
                            "content": m.get("content", "")})
            else:
                out.append({"role": m.role, "content": m.content})
        return out
    return [{"role": "user", "content": prompt}]


def _joined_prompt(prompt: str, messages: Sequence[ChatMessage | dict] | None) -> str:
    if not messages:
        return prompt
    parts = []
    for m in messages:
        if isinstance(m, dict):
            parts.append(f"{m.get('role', 'user')}: {m.get('content', '')}")
        else:
            parts.append(f"{m.role}: {m.content}")
    return "\n".join(parts)


class BaseRuntime:
    def __init__(self) -> None:
        self._cancel = asyncio.Event()

    async def health(self) -> bool:
        raise NotImplementedError

    async def infer_stream(self, prompt: str, max_tokens: int,
                           messages: Sequence[ChatMessage | dict] | None = None
                           ) -> AsyncIterator[str]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def cancel(self) -> None:
        self._cancel.set()


class EchoRuntime(BaseRuntime):
    """Echoes the prompt back word by word with a small delay."""

    def __init__(self, delay_s: float = 0.005) -> None:
        super().__init__()
        self.delay_s = delay_s

    async def health(self) -> bool:
        return True

    async def infer_stream(self, prompt: str, max_tokens: int,
                           messages: Sequence[ChatMessage | dict] | None = None
                           ) -> AsyncIterator[str]:
        self._cancel.clear()
        text = _joined_prompt(prompt, messages)
        words = text.split() or ["(empty)"]
        yield f"echo[{len(words)}w]: "
        for w in words[:max_tokens]:
            if self._cancel.is_set():
                return
            await asyncio.sleep(self.delay_s)
            yield w + " "


class OpenAICompatRuntime(BaseRuntime):
    """Streams from an OpenAI-compatible endpoint on localhost."""

    def __init__(self, base_url: str, model_name: str, timeout_s: float = 300.0) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.timeout_s = timeout_s

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self.base_url}/models")
                return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def infer_stream(self, prompt: str, max_tokens: int,
                           messages: Sequence[ChatMessage | dict] | None = None
                           ) -> AsyncIterator[str]:
        self._cancel.clear()
        body = {
            "model": self.model_name,
            "messages": _as_openai_messages(prompt, messages),
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                async with c.stream("POST", f"{self.base_url}/chat/completions", json=body) as r:
                    if r.status_code != 200:
                        text = (await r.aread()).decode(errors="replace")[:500]
                        raise ModelNotReadyError(f"backend {r.status_code}: {text}")
                    async for line in r.aiter_lines():
                        if self._cancel.is_set():
                            return
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            delta = json.loads(data)["choices"][0]["delta"].get("content")
                        except (KeyError, IndexError, json.JSONDecodeError):
                            continue
                        if delta:
                            yield delta
        except httpx.HTTPError as e:
            raise ModelNotReadyError(f"backend unreachable: {e}") from e


def build_runtime(runtime: str, base_url: str = "", model_name: str = "") -> BaseRuntime:
    if runtime == "echo":
        return EchoRuntime()
    if runtime == "openai-compat":
        return OpenAICompatRuntime(base_url, model_name)
    raise ModelNotReadyError(f"unknown runtime: {runtime}")


def capabilities_for_family(family: str) -> list[TaskType]:
    """Cheap capability tags from the model family name, used by auto routing."""
    name = family.lower()
    if any(k in name for k in ("embed", "nomic", "e5-", "bge")):
        return [TaskType.EMBEDDING]
    caps = [TaskType.CHAT]
    if any(k in name for k in ("coder", "code", "starcoder", "deepseek")):
        caps.append(TaskType.CODE_GENERATION)
    if any(k in name for k in ("summari",)):
        caps.append(TaskType.SUMMARIZATION)
    return caps


def spec_from_config(cfg: "NodeConfig") -> ModelSpec:  # noqa: F821
    from common.config import NodeConfig  # noqa: F401
    return ModelSpec(
        family=cfg.model_family,
        parameter_count_b=cfg.model_parameter_b,
        active_parameter_count_b=cfg.model_active_parameter_b,
        runtime=cfg.model_runtime,
        context_window=cfg.context_window,
        parallel_slots=cfg.parallel_slots,
        capabilities=capabilities_for_family(cfg.model_family),
    )
