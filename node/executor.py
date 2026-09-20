"""Node task executor — single-shot inference plus bounded tool loop.

Single-shot (default): one infer_stream call, streamed live as tokens
arrive (WS progress / progress_cb) and returned as-is at the end.
The server harness extracts the diff. Used for echo/small models and
the vLLM Nemotron path.

Tool loop (opt-in per task): the assignment prompt carries a
``TOOLS:`` block; the executor seeds a Sandbox from the embedded file
snapshot, runs up to MAX_STEPS inference rounds, streams each round
live with a ``[step N]`` header, applies each round's tool calls
deterministically, feeds observations back, and ends with a final
diff round. The server still reverifies everything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from pathlib import Path

from common.types import TaskAssignment, TaskRequest
from node.model_runtime import BaseRuntime
from node.tools import MAX_STEPS, Sandbox, ToolError, parse_tool_calls

log = logging.getLogger("clique.executor")

# Live-stream flush tuning: forward progress after this many buffered
# chars, or this long since the last flush, whichever comes first.
# Small enough for sub-second first-token visibility on the dash/CLI
# (~0.5s at 46 TPS single-stream), large enough to avoid WS flooding
# with 16 parallel_slots (16 tasks x ~2 msg/s worst case).
FLUSH_CHARS = 200
FLUSH_INTERVAL_S = 0.5
WS_CHUNK = 2000

TOOL_PREAMBLE = (
    "You have file tools. Emit tool calls as single-line JSON objects "
    'with an "op" key: {"op":"read","path":"..."}, '
    '{"op":"edit","path":"...","old":"...","new":"..."}, '
    '{"op":"write","path":"...","content":"..."}, '
    '{"op":"bash","cmd":["pytest","-q"]}, or {"op":"done"}. '
    "One call per line, no other JSON. When finished, emit the final "
    "unified diff in a single ```diff fence."
)


async def _send(ws, assignment: TaskAssignment, progress_cb,
                offset: int, text: str) -> None:
    """Forward one progress slice via callback, WS, or nowhere."""
    if not text:
        return
    if progress_cb is not None:
        res = progress_cb(text)
        if asyncio.iscoroutine(res):
            await res
        return
    if ws is None:
        return
    from common import protocol
    for i in range(0, len(text), WS_CHUNK):
        await ws.send(protocol.dumps(protocol.msg_progress(
            assignment.task_id, assignment.attempt_id, offset + i,
            text[i:i + WS_CHUNK])))


async def _collect_streaming(runtime: BaseRuntime, prompt: str,
                             max_tokens: int, ws,
                             assignment: TaskAssignment,
                             progress_cb=None, prefix: str = "") -> str:
    """Consume infer_stream, forwarding live progress as tokens arrive.

    Returns the full text. ``prefix`` (e.g. ``[step N] ``) is sent first
    as progress-only framing but is NOT part of the return value, so
    tool-loop transcripts stay parseable. Small outputs flush once at
    the end (one callback per round, as before); long generations
    flush incrementally every FLUSH_CHARS / FLUSH_INTERVAL_S.
    """
    chunks: list[str] = []
    buf = prefix
    offset = 0
    last_flush = time.monotonic()
    async for delta in runtime.infer_stream(prompt, max_tokens):
        chunks.append(delta)
        buf += delta
        now = time.monotonic()
        if len(buf) >= FLUSH_CHARS or (buf and now - last_flush >= FLUSH_INTERVAL_S):
            await _send(ws, assignment, progress_cb, offset, buf)
            offset += len(buf)
            buf = ""
            last_flush = now
    if buf:
        await _send(ws, assignment, progress_cb, offset, buf)
    return "".join(chunks)


async def _collect(runtime: BaseRuntime, prompt: str,
                   max_tokens: int) -> str:
    """Non-streaming collect (tests / execute_simple without WS)."""
    chunks: list[str] = []
    async for delta in runtime.infer_stream(prompt, max_tokens):
        chunks.append(delta)
    return "".join(chunks)


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
                  progress_cb=None, workspace_drift=None,
                  call_tool=None) -> str:
    """Run one task. Returns the final raw output text.

    Streams live: each infer_stream delta is forwarded (progress_cb
    when ws is None, else agent WS progress) in ~200-char slices, so
    the dash/CLI/SSE show tokens as they generate instead of waiting
    for completion. Returns the full text at the end.
    workspace_drift() -> list[str] | None: optional callback returning
    paths changed in the live workspace since the task snapshot. When
    non-empty, a drift note is appended to the transcript so the model
    rereads those files instead of reasoning on stale content.
    call_tool(name, arguments) -> (ok, result): optional callback that
    executes one model-requested tool call server-side (agent relays
    over WS). When request.tools is set, the executor runs an agentic
    loop (infer_tools rounds) instead of single-shot streaming.
    """
    if request.tools and call_tool is not None:
        return await _execute_agentic(
            ws, runtime, assignment, request, progress_cb, call_tool)
    use_tools = "TOOLS:" in request.prompt
    if not use_tools:
        return await _collect_streaming(
            runtime, request.prompt, request.max_output_tokens,
            ws, assignment, progress_cb)

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
            out = await _collect_streaming(
                runtime, transcript, request.max_output_tokens,
                ws, assignment, progress_cb, prefix=f"[step {step}] ")
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


#: Max model->tool->model rounds per task (bounds cost on small models
#: that call tools in circles).
AGENTIC_MAX_ROUNDS = 8


async def _execute_agentic(ws, runtime: BaseRuntime,
                           assignment: TaskAssignment, request: TaskRequest,
                           progress_cb, call_tool) -> str:
    """OpenAI function-call loop: model emits tool_calls, server executes.

    Each round: one blocking infer_tools call with the full transcript,
    then every requested call runs via call_tool (server-side relay).
    Observations append as role=tool messages. Ends when the model
    returns content with no calls, or rounds exhaust (last content wins).
    Progress streams per-round headers so the dash shows the loop live.
    """
    from node.model_runtime import _as_openai_messages, _as_openai_tools
    tools = _as_openai_tools(request.tools)
    transcript: list[dict] = _as_openai_messages(
        request.prompt, request.messages)
    # Nudge text-only models toward the fallback shape the parser
    # catches ({"action": name, "arguments": {...}}).
    names = [t.name for t in (request.tools or [])]
    transcript[0]["content"] = (
        transcript[0].get("content", "")
        + "\nYou have tools: " + ", ".join(names) + ". "
        "To use one, reply with ONLY a JSON object "
        '{"action": "<name>", "arguments": {...}} and nothing else. '
        "Results come back as a tool message; then continue.")
    final_content = ""
    for rnd in range(AGENTIC_MAX_ROUNDS):
        content, calls = await runtime.infer_tools(
            transcript, tools, request.tool_choice,
            request.max_output_tokens)
        if not calls:
            final_content = content
            if content:
                await _send(ws, assignment, progress_cb, 0,
                            f"[tools done after {rnd} calls] ")
            break
        await _send(ws, assignment, progress_cb, 0,
                    f"[step {rnd}] {len(calls)} tool call(s): "
                    + ", ".join(c.name for c in calls) + " ")
        # assistant turn with tool_calls goes on the transcript first
        transcript.append({
            "role": "assistant", "content": content or "",
            "tool_calls": [
                {"id": c.call_id or f"call_{rnd}_{i}",
                 "type": "function",
                 "function": {"name": c.name,
                              "arguments": json.dumps(c.arguments)}}
                for i, c in enumerate(calls)]})
        for i, c in enumerate(calls[:4]):  # cap fan-out per round
            call_id = c.call_id or f"call_{rnd}_{i}"
            try:
                ok, result = await call_tool(c.name, c.arguments)
            except Exception as e:
                ok, result = False, f"relay error: {e}"
            if not isinstance(result, str):
                try:
                    result = json.dumps(result)
                except (TypeError, ValueError):
                    result = str(result)
            transcript.append({"role": "tool", "content": result[:8000],
                               "tool_call_id": call_id})
            await _send(ws, assignment, progress_cb, 0,
                        f"[tool {c.name}: {'ok' if ok else 'error'}] ")
        final_content = content
    return final_content or "(no answer)"
