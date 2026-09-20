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


# -- WS relay integration -------------------------------------------------
# A scripted tool-capable runtime on a REAL NodeAgent over a REAL agent WS:
# model emits a call -> agent relays TOOL_CALL -> server executes ->
# TOOL_RESULT returns -> model answers. No mocks of the relay path.

class RelayRuntime:
    """First infer_tools round requests workspace_create; second answers."""

    def __init__(self) -> None:
        self.rounds = 0

    async def health(self) -> bool:
        return True

    async def infer_stream(self, prompt, max_tokens, messages=None):
        yield ""

    async def infer_tools(self, messages, tools, tool_choice, max_tokens):
        from common.types import ToolCall
        self.rounds += 1
        if self.rounds == 1:
            return "", [ToolCall(call_id="c1",
                                 name="clique_workspace_create",
                                 arguments={"files": {"relay.md": "via ws\n"}})]
        return "created", []


@pytest.mark.asyncio
async def test_ws_relay_end_to_end(tmp_path):
    """Full relay over real agent WS with a tool-capable runtime."""
    import asyncio
    import socket
    import uuid

    import httpx
    import uvicorn

    from common.config import Config
    from common.types import TaskRequest, ToolDef
    from node.agent import NodeAgent

    def make_config(name: str, port: int) -> Config:
        cfg = Config()
        cfg.node.display_name = name
        cfg.node.data_dir = tmp_path / name
        cfg.node.model_runtime = "echo"
        cfg.node.model_family = "echo"
        cfg.node.heartbeat_interval_s = 0.2
        cfg.server.api_host = "127.0.0.1"
        cfg.server.api_port = port
        cfg.server.db_path = tmp_path / "server.db"
        cfg.server.lease_seconds = 30.0
        return cfg

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = make_server(tmp_path)
    uv = uvicorn.Server(uvicorn.Config(
        server.app, host="127.0.0.1", port=port, log_level="warning"))
    server_task = asyncio.create_task(uv.serve())
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            for _ in range(100):
                try:
                    await c.get(base + "/v1/clique")
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)
        agent = NodeAgent(make_config("relay-node", port), base)
        agent.runtime = RelayRuntime()  # tool-capable stand-in
        agent_task = asyncio.create_task(agent.run())
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                for _ in range(100):
                    nodes = (await c.get(base + "/v1/nodes")).json()
                    if any(n["status"] == "ready" for n in nodes):
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("agent never ready")
                req = TaskRequest(
                    prompt="create a workspace", idempotency_key=uuid.uuid4().hex,
                    tools=[ToolDef(name="clique_workspace_create",
                                   description="create",
                                   parameters={"type": "object",
                                               "properties": {}})])
                r = await c.post(base + "/v1/tasks",
                                 json=req.model_dump(mode="json"))
                task_id = r.json()["task_id"]
                for _ in range(200):
                    data = (await c.get(
                        f"{base}/v1/tasks/{task_id}")).json()
                    if data["state"] in ("succeeded", "failed",
                                         "cancelled", "expired"):
                        break
                    await asyncio.sleep(0.1)
                assert data["state"] == "succeeded", data
                assert data["result"]["output"] == "created"
                # the relay actually ran server-side: workspace exists
                listed = (await c.get(base + "/v1/workspaces")).json()
                assert len(listed) == 1
                wid = listed[0]["workspace_id"]
                st = (await c.get(f"{base}/v1/workspaces/{wid}/file",
                                  params={"path": "relay.md"})).json()
                assert st["text"] == "via ws\n"
        finally:
            agent._stop.set()
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
    finally:
        uv.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
