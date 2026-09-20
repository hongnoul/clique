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
