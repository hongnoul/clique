"""Node task executor — single-shot inference plus bounded tool loop.

Single-shot (default): one infer_stream call, output returned as-is.
The server harness extracts the diff. Used for echo/small models.

Tool loop (opt-in per task): the assignment prompt carries a
``TOOLS:`` block; the executor seeds a Sandbox from the embedded file
snapshot, runs up to MAX_STEPS inference rounds, applies each round's
tool calls deterministically, feeds observations back, and ends with a
final diff round. The server still reverifies everything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import AsyncIterator

from common.types import TaskAssignment, TaskRequest
from node.model_runtime import BaseRuntime
from node.tools import MAX_STEPS, Sandbox, ToolError, parse_tool_calls

log = logging.getLogger("clique.executor")

TOOL_PREAMBLE = (
    "You have file tools. Emit tool calls as single-line JSON objects "
    'with an "op" key: {"op":"read","path":"..."}, '
    '{"op":"edit","path":"...","old":"...","new":"..."}, '
    '{"op":"write","path":"...","content":"..."}, '
    '{"op":"bash","cmd":["pytest","-q"]}, or {"op":"done"}. '
    "One call per line, no other JSON. When finished, emit the final "
    "unified diff in a single ```diff fence."
)


async def _collect(runtime: BaseRuntime, prompt: str,
                   max_tokens: int) -> str:
    chunks: list[str] = []
    async for delta in runtime.infer_stream(prompt, max_tokens):
        chunks.append(delta)
    return "".join(chunks)


async def _emit(ws, assignment: TaskAssignment, text: str,
                progress_cb=None) -> None:
    """Forward text for dash visibility: via WS, callback, or nowhere."""
    if progress_cb is not None:
        res = progress_cb(text)
        if asyncio.iscoroutine(res):
            await res
        return
    if ws is None:
        return
    await _stream_all(ws, assignment, text)


async def _stream_text(ws, assignment: TaskAssignment,
                       text: str) -> AsyncIterator[None]:
    """Yield per chunk while forwarding progress deltas (chunked)."""
    from common import protocol
    for i in range(0, len(text), 2000):
        yield None
        await ws.send(protocol.dumps(protocol.msg_progress(
            assignment.task_id, assignment.attempt_id, i,
            text[i:i + 2000])))


async def _stream_all(ws, assignment: TaskAssignment, text: str) -> None:
    async for _ in _stream_text(ws, assignment, text):
        pass


def _snapshot_from_prompt(prompt: str) -> dict[str, str]:
    """Recover the file snapshot embedded by build_code_prompt.

    Falls back to empty (single-shot path) when no snapshot present.
    """
    files: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for line in prompt.splitlines():
        if line.startswith("--- file: ") and line.endswith(" ---"):
            if cur is not None:
                files[cur] = "\n".join(buf).strip("\n") + "\n"
            cur = line[len("--- file: "):-len(" ---")]
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        files[cur] = "\n".join(buf).strip("\n") + "\n"
    # The prompt packs task header before "Files:"; strip trailing sections.
    return files


async def execute(ws, runtime: BaseRuntime, assignment: TaskAssignment,
                  request: TaskRequest,
                  progress_cb=None, workspace_drift=None) -> str:
    """Run one task. Returns the final raw output text.

    progress_cb(text) is awaited for each streamed chunk when ws is None
    (tests); otherwise progress goes over the agent WS.
    workspace_drift() -> list[str] | None: optional callback returning
    paths changed in the live workspace since the task snapshot. When
    non-empty, a drift note is appended to the transcript so the model
    rereads those files instead of reasoning on stale content.
    """
    use_tools = "TOOLS:" in request.prompt
    if not use_tools:
        text = await _collect(runtime, request.prompt,
                              request.max_output_tokens)
        await _emit(ws, assignment, text, progress_cb)
        return text

    files = _snapshot_from_prompt(request.prompt)
    with tempfile.TemporaryDirectory(prefix="clique-tools-") as tmp:
        sandbox = Sandbox(Path(tmp))
        if files:
            try:
                sandbox.seed(files)
            except ToolError as e:
                log.warning("sandbox seed failed: %s", e)
        transcript = request.prompt + "\n" + TOOL_PREAMBLE + "\n"
        final = ""
        for step in range(MAX_STEPS + 1):
            if workspace_drift is not None:
                try:
                    drifted = workspace_drift()
                except Exception:
                    drifted = None
                if drifted:
                    names = ", ".join(sorted(set(drifted))[:8])
                    transcript += (
                        "\nNote: live workspace changed under you since "
                        f"your snapshot (step {step}): {names}. "
                        "Re-read those files with the read tool before "
                        "editing them.\n")
            out = await _collect(runtime, transcript,
                                 request.max_output_tokens)
            await _emit(ws, assignment, f"[step {step}] {out[:2000]}",
                        progress_cb)
            final = out
            try:
                calls = parse_tool_calls(out)
            except ToolError as e:
                transcript += f"\nObservation: tool parse error: {e}\n"
                continue
            if not calls or any(c.get("op") == "done" for c in calls):
                break
            obs: list[str] = []
            for call in calls[:8]:  # cap fan-out per round
                try:
                    res = sandbox.apply_call(call)
                except ToolError as e:
                    res = f"error: {e}"
                obs.append(f"{call.get('op')}: {res[:1500]}")
            transcript += "\nObservation:\n" + "\n".join(obs) + "\nContinue.\n"
        # Append the verified-local snapshot hash so the server can
        # short-circuit identical proposals (best-effort, advisory).
        try:
            snap = sandbox.snapshot()
            if snap:
                final += "\n<!-- sandbox files: " + \
                    json.dumps(sorted(snap)) + " -->\n"
        except Exception:
            pass
        return final


async def execute_simple(runtime: BaseRuntime, prompt: str,
                         max_tokens: int) -> str:
    """Test helper: single-shot without WS."""
    return await _collect(runtime, prompt, max_tokens)
