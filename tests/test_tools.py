"""Tests for node-local tool sandbox + bounded executor loop."""

from __future__ import annotations

import json

import pytest

from common.types import TaskAssignment, TaskRequest
from node.executor import execute
from node.tools import Sandbox, ToolError, parse_tool_calls


def test_parse_skips_fences_and_prose():
    text = ('thinking...\n```diff\n{"op":"edit"}\n```\n'
            '{"op": "read", "path": "a.py"}\nnot json\n')
    assert parse_tool_calls(text) == [{"op": "read", "path": "a.py"}]


def test_parse_bad_json():
    with pytest.raises(ToolError, match="bad_tool_json"):
        parse_tool_calls('{"op": "read", bad}\n')


def test_sandbox_edit_exact_once(tmp_path):
    sb = Sandbox(tmp_path / "s")
    sb.seed({"a.py": "x = 1\ny = 2\n"})
    assert sb.edit("a.py", "x = 1", "x = 9") == "edited a.py"
    with pytest.raises(ToolError, match="edit_ambiguous"):
        sb.edit("a.py", " = ", ": ")  # matches multiple times


def test_sandbox_path_escape(tmp_path):
    sb = Sandbox(tmp_path / "s")
    sb.seed({"a.py": "x"})
    with pytest.raises(ToolError, match="path_forbidden"):
        sb.read("../evil.py")
    with pytest.raises(ToolError, match="write_refused_overwrite"):
        sb.write("a.py", "new")


def test_sandbox_bash_allowlist(tmp_path):
    sb = Sandbox(tmp_path / "s")
    sb.seed({"t.py": "x"})
    with pytest.raises(ToolError, match="bash_forbidden"):
        sb.bash(["rm", "-rf", "."])


class ScriptRuntime:
    """Yield scripted outputs, one per infer_stream call."""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)

    async def health(self) -> bool:
        return True

    async def infer_stream(self, prompt: str, max_tokens: int):
        yield self.outputs.pop(0) if self.outputs else '{"op":"done"}\n'

    async def cancel(self) -> None:
        pass


def _req(prompt: str) -> TaskRequest:
    return TaskRequest(prompt=prompt, idempotency_key="k")


def _asgn() -> TaskAssignment:
    from datetime import datetime, timezone
    return TaskAssignment(task_id="t", attempt_id="a", node_id="n",
                          lease_expires_at=datetime.now(timezone.utc),
                          reason="test")


@pytest.mark.asyncio
async def test_single_shot_passthrough():
    rt = ScriptRuntime(["plain output, no tools"])
    out = await execute(None, rt, _asgn(), _req("hello"))
    assert out == "plain output, no tools"


@pytest.mark.asyncio
async def test_single_shot_streams_live_chunks():
    """Long generations flush incrementally, not just once at the end."""

    class ChunkRuntime:
        async def health(self):
            return True

        async def infer_stream(self, prompt, max_tokens):
            for i in range(60):
                yield f"tok{i} "

        async def cancel(self):
            pass

    seen: list[str] = []
    out = await execute(None, ChunkRuntime(), _asgn(), _req("hello"),
                        progress_cb=seen.append)
    assert out == "".join(f"tok{i} " for i in range(60))
    assert "".join(seen) == out  # no progress lost
    assert len(seen) > 1  # flushed incrementally, not buffered to the end
    assert len(seen) < 60  # but batched, not one WS msg per token
    # small outputs keep the old behavior: exactly one callback
    seen2: list[str] = []
    out2 = await execute(None, ScriptRuntime(["tiny"]), _asgn(),
                         _req("hi"), progress_cb=seen2.append)
    assert out2 == "tiny" and seen2 == ["tiny"]


@pytest.mark.asyncio
async def test_single_shot_streams_over_ws():
    """Without progress_cb, chunks go over the agent WS as msg_progress."""
    import json as _json

    class ChunkRuntime:
        async def health(self):
            return True

        async def infer_stream(self, prompt, max_tokens):
            for i in range(60):
                yield f"tok{i} "

        async def cancel(self):
            pass

    class FakeWS:
        def __init__(self):
            self.sent: list[str] = []

        async def send(self, raw: str):
            self.sent.append(raw)

    ws = FakeWS()
    out = await execute(ws, ChunkRuntime(), _asgn(), _req("hello"))
    assert out.startswith("tok0 ")
    assert len(ws.sent) > 1
    first = _json.loads(ws.sent[0])
    assert first["type"] == "progress" and first["token_offset"] == 0
    body = "".join(_json.loads(s)["text_delta"] for s in ws.sent)
    assert body == out


@pytest.mark.asyncio
async def test_tool_loop_edits_then_dones():
    rt = ScriptRuntime([
        '{"op": "read", "path": "foo.py"}\n',
        '{"op": "edit", "path": "foo.py", "old": "old", "new": "new"}\n',
        'final\n```diff\n--- a/foo.py\n+++ b/foo.py\n@@\n-old\n+new\n```\n',
    ])
    prompt = ("TOOLS:\nfix it\n--- file: foo.py ---\nold\n")
    seen: list[str] = []
    out = await execute(None, rt, _asgn(), _req(prompt),
                        progress_cb=seen.append)
    assert "```diff" in out
    assert len(seen) == 3  # one progress callback per round


@pytest.mark.asyncio
async def test_tool_loop_caps_steps():
    rt = ScriptRuntime(
        ['{"op": "read", "path": "foo.py"}\n'] * 20)
    prompt = "TOOLS:\nloop\n--- file: foo.py ---\nold\n"
    out = await execute(None, rt, _asgn(), _req(prompt),
                        progress_cb=lambda t: None)
    assert out  # terminates via MAX_STEPS, never hangs


# -- agentic function-call loop ------------------------------------------

class ScriptToolsRuntime:
    """Scripted (content, calls) rounds for infer_tools."""

    def __init__(self, rounds: list[tuple[str, list]]) -> None:
        self.rounds = list(rounds)

    async def health(self) -> bool:
        return True

    async def infer_stream(self, prompt: str, max_tokens: int):
        yield ""

    async def infer_tools(self, messages, tools, tool_choice, max_tokens):
        return self.rounds.pop(0) if self.rounds else ("done", [])


def _agentic_req() -> TaskRequest:
    from common.types import ToolDef
    return TaskRequest(
        prompt="assess the system", idempotency_key="k",
        tools=[ToolDef(name="clique_self_assess",
                       description="assess",
                       parameters={"type": "object", "properties": {}})])


@pytest.mark.asyncio
async def test_agentic_loop_calls_tool_and_returns_answer():
    from common.types import ToolCall
    rt = ScriptToolsRuntime([
        ("", [ToolCall(call_id="c1", name="clique_self_assess",
                       arguments={})]),
        ("all healthy", []),
    ])
    relayed: list = []

    async def fake_call(name, args):
        relayed.append((name, args))
        return True, '{"findings": ["healthy"]}'
    out = await execute(None, rt, _asgn(), _agentic_req(),
                        progress_cb=lambda t: None, call_tool=fake_call)
    assert out == "all healthy"
    assert relayed == [("clique_self_assess", {})]


@pytest.mark.asyncio
async def test_agentic_loop_text_fallback_counts_as_call():
    # model describes the call as literal JSON text (lobster mode):
    # parser must still route it to the tool, not the answer.
    from node.model_runtime import parse_tool_calls_response
    content, calls = parse_tool_calls_response({
        "choices": [{"message": {
            "content": '{"action": "clique_self_assess", "arguments": {}}'}}]})
    assert content == "" and calls[0].name == "clique_self_assess"


@pytest.mark.asyncio
async def test_agentic_loop_native_tool_calls_parsed():
    from node.model_runtime import parse_tool_calls_response
    content, calls = parse_tool_calls_response({
        "choices": [{"message": {
            "content": "",
            "tool_calls": [{"id": "c9", "type": "function",
                            "function": {"name": "clique_status",
                                         "arguments": '{"verbose": true}'}}]}}]})
    assert calls[0].call_id == "c9"
    assert calls[0].arguments == {"verbose": True}


@pytest.mark.asyncio
async def test_agentic_loop_without_relay_falls_back_to_single_shot():
    # request.tools set but no call_tool (e.g. tests): plain path, no hang
    rt = ScriptToolsRuntime([("should not be used", [])])
    out = await execute(None, rt, _asgn(), _agentic_req(),
                        progress_cb=lambda t: None)
    assert out == ""


@pytest.mark.asyncio
async def test_fallback_after_think_block():
    # nemotron wraps answers after </think>: parser must find the JSON
    from node.model_runtime import parse_tool_calls_response
    content, calls = parse_tool_calls_response({
        "choices": [{"message": {
            "content": "We need to respond with only a JSON object.\n</think>\n"
                       '{"action": "clique_self_assess", "arguments": {}}'}}]})
    assert content == "" and calls[0].name == "clique_self_assess"


@pytest.mark.asyncio
async def test_fallback_accepts_tool_key_and_string_args():
    from node.model_runtime import parse_tool_calls_response
    content, calls = parse_tool_calls_response({
        "choices": [{"message": {
            "content": '{\n  "tool": "clique_self_assess",\n  "arguments": {}\n}'}}]})
    assert content == "" and calls[0].name == "clique_self_assess"


@pytest.mark.asyncio
async def test_fallback_xml_tool_call_shape():
    # nemotron's native text shape: <tool_call><function=name>..</function></tool_call>
    from node.model_runtime import parse_tool_calls_response
    content, calls = parse_tool_calls_response({
        "choices": [{"message": {
            "content": "thinking...\n</think>\n<tool_call>\n"
                       "<function=clique_self_assess>\n</function>\n</tool_call>\n"}}]})
    assert content == "" and calls[0].name == "clique_self_assess"
    assert calls[0].arguments == {}
