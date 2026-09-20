"""End-to-end agentic loop: adapter forwards tools, server executes.

Live server + echo agent path is single-shot only (echo has no tools),
so this suite drives the two halves with fakes:
- chat_completions carries body.tools into TaskRequest.tools
- execute_tool runs workspace ops against a real SchedulerServer
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from common.config import Config
from scheduler.server import SchedulerServer
from scheduler.tool_executor import execute_tool


def make_server(tmp_path) -> SchedulerServer:
    cfg = Config()
    cfg.node.display_name = "test-server"
    cfg.node.data_dir = tmp_path
    cfg.server.db_path = tmp_path / "server.db"
    return SchedulerServer(cfg)


@pytest.mark.asyncio
async def test_execute_tool_workspace_roundtrip(tmp_path):
    server = make_server(tmp_path)
    ok, created = await execute_tool(server, "t-1", "clique_workspace_create",
                                     {"files": {"a.md": "hi\n"}})
    assert ok
    wid = created["workspace_id"]
    ok, ev = await execute_tool(server, "t-1", "clique_workspace_write",
                                {"workspace_id": wid, "path": "a.md",
                                 "text": "hello\n"})
    assert ok and ev["version"] == 2
    ok, read = await execute_tool(server, "t-1", "clique_workspace_read",
                                  {"workspace_id": wid, "path": "a.md"})
    assert ok and read["text"] == "hello\n"
    ok, bundle = await execute_tool(server, "t-1", "clique_workspace_export",
                                    {"workspace_id": wid})
    assert ok and bundle["files"]["a.md"] == "hello\n"
    assert bundle["workspace_id"] == wid


@pytest.mark.asyncio
async def test_execute_tool_unknown_and_missing(tmp_path):
    server = make_server(tmp_path)
    ok, err = await execute_tool(server, "t-1", "rm_rf", {})
    assert not ok and "unknown tool" in err
    ok, err = await execute_tool(server, "t-1", "clique_workspace_read",
                                 {"workspace_id": "nope", "path": "a"})
    assert not ok  # KeyError -> clean failure, no traceback


@pytest.mark.asyncio
async def test_execute_tool_self_assess_shape(tmp_path):
    server = make_server(tmp_path)
    ok, report = await execute_tool(server, "t-1", "clique_self_assess", {})
    assert ok
    assert report["server"]["sha"]
    assert "findings" in report and report["fleet"]["total"] >= 0


def test_chat_completions_forwards_tools():
    """Adapter tool parsing: body.tools -> ToolDefs + tool_choice."""
    from scheduler.api.rest import _parse_chat_tools
    tools = [{"type": "function",
              "function": {"name": "clique_self_assess",
                           "description": "assess",
                           "parameters": {"type": "object",
                                          "properties": {}}}}]
    parsed, choice = _parse_chat_tools(
        {"tools": tools, "tool_choice": "auto"})
    assert [t.name for t in parsed] == ["clique_self_assess"]
    assert choice == "auto"
    # empty / malformed bodies parse to nothing
    assert _parse_chat_tools({}) == ([], None)
    assert _parse_chat_tools({"tools": [{"type": "function",
                                          "function": {}}]}) == ([], None)
    # TaskRequest carries the manifest end to end (agent receives it)
    from common.types import TaskRequest
    req = TaskRequest(prompt="hi", idempotency_key="k",
                      tools=parsed, tool_choice=choice)
    wire = req.model_dump(mode="json")
    assert wire["tools"][0]["name"] == "clique_self_assess"
    assert wire["tool_choice"] == "auto"
    back = TaskRequest.model_validate(wire)
    assert back.tools and back.tools[0].name == "clique_self_assess"
