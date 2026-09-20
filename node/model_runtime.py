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
from common.types import ChatMessage, ModelSpec, TaskType, ToolCall, ToolDef


def _as_openai_messages(
        prompt: str, messages: Sequence[ChatMessage | dict] | None) -> list[dict]:
    if messages:
        out = []
        for m in messages:
            if isinstance(m, dict):
                entry: dict = {"role": m.get("role", "user"),
                               "content": m.get("content", "")}
                if m.get("tool_calls"):
                    entry["tool_calls"] = m["tool_calls"]
                if m.get("tool_call_id"):
                    entry["tool_call_id"] = m["tool_call_id"]
                    if m.get("name"):
                        entry["name"] = m["name"]
                out.append(entry)
            else:
                entry = {"role": m.role, "content": m.content}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"id": tc.call_id or f"call_{i}",
                         "type": "function",
                         "function": {"name": tc.name,
                                      "arguments": json.dumps(tc.arguments)}}
                        for i, tc in enumerate(m.tool_calls)]
                if m.tool_call_id:
                    entry["tool_call_id"] = m.tool_call_id
                out.append(entry)
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


def _as_openai_tools(tools: Sequence[ToolDef | dict] | None) -> list[dict] | None:
    """ToolDef list -> OpenAI tools array. None when no tools offered."""
    if not tools:
        return None
    out = []
    for t in tools:
        if isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name", "")
            desc = t.get("description", "")
            params = t.get("parameters")
            if params is None and isinstance(t.get("function"), dict):
                f = t["function"]
                desc = desc or f.get("description", "")
                params = f.get("parameters", {})
            if not name:
                continue
            out.append({"type": "function",
                        "function": {"name": name, "description": desc,
                                     "parameters": params or {}}})
        else:
            out.append({"type": "function",
                        "function": {"name": t.name,
                                     "description": t.description,
                                     "parameters": t.parameters or {}}})
    return out or None


def _fallback_call(obj: dict) -> ToolCall | None:
    """One ToolCall from a describe-instead-of-call JSON object, or None."""
    if not isinstance(obj, dict):
        return None
    name = obj.get("action") or obj.get("name") or obj.get("tool") \
        or obj.get("function") or ""
    args = obj.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if name and isinstance(args, dict):
        return ToolCall(call_id="", name=name, arguments=args)
    return None


def parse_tool_calls_response(data: dict) -> tuple[str, list[ToolCall]]:
    """Split one non-streaming choice into (content, tool_calls).

    Handles both native ``tool_calls`` and the text fallback where a
    small model emits ``{"action": "<name>", "arguments": {...}}`` as
    literal text (describe-instead-of-call).
    """
    calls: list[ToolCall] = []
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError):
        return "", []
    msg = choice.get("message", {})
    content = msg.get("content") or ""
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments", "")}
        if not isinstance(args, dict):
            args = {"_raw": args}
        calls.append(ToolCall(call_id=tc.get("id", ""),
                              name=fn.get("name", ""), arguments=args))
    if not calls and content.strip().startswith("{"):
        try:
            obj = json.loads(content.strip())
        except json.JSONDecodeError:
            obj = None
        fb = _fallback_call(obj or {})
        if fb is not None:
            calls = [fb]
            content = ""
    if not calls and "</think>" in content:
        # reasoning models wrap the answer after </think>: the fallback
        # JSON may follow the think block instead of starting the text.
        from common.think import split_think
        _, tail = split_think(content)
        tail = tail.strip()
        if tail.startswith("{"):
            try:
                obj = json.loads(tail)
            except json.JSONDecodeError:
                obj = None
            fb = _fallback_call(obj or {})
            if fb is not None:
                calls = [fb]
                content = ""
    return content, calls


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

    async def infer_tools(
            self, messages: list[dict], tools: list[dict] | None,
            tool_choice: str | dict | None,
            max_tokens: int) -> tuple[str, list[ToolCall]]:
        """One non-streaming round with tools. Returns (content, calls)."""
        raise NotImplementedError


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

    async def infer_tools(
            self, messages: list[dict], tools: list[dict] | None,
            tool_choice: str | dict | None,
            max_tokens: int) -> tuple[str, list[ToolCall]]:
        return "(echo has no tools)", []


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

    async def infer_tools(
            self, messages: list[dict], tools: list[dict] | None,
            tool_choice: str | dict | None,
            max_tokens: int) -> tuple[str, list[ToolCall]]:
        """One blocking round offering tools to the backend (vLLM)."""
        self._cancel.clear()
        body: dict = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            # NOTE: fleet vLLM runs without --enable-auto-tool-choice /
            # --tool-call-parser, so "auto" 400s. Only forward explicit
            # non-auto choices; the transcript nudge + text fallback
            # parser carry the loop on this fleet.
            if tool_choice and tool_choice != "auto":
                body["tool_choice"] = tool_choice
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.post(f"{self.base_url}/chat/completions",
                                 json=body)
                if r.status_code != 200:
                    text = r.text[:500]
                    raise ModelNotReadyError(f"backend {r.status_code}: {text}")
                data = r.json()
        except httpx.HTTPError as e:
            raise ModelNotReadyError(f"backend unreachable: {e}") from e
        return parse_tool_calls_response(data)


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
