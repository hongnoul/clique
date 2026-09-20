"""Tests for the clique MCP server (client/mcp_server.py).

Reuses the live-server fixture style from test_extended: real uvicorn +
real echo agents. Tools are driven through ``MCPServer.call_tool`` which
exercises schema validation and dispatch without stdio plumbing.
"""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
import pytest_asyncio
import uvicorn

import client.mcp_server as mcp_mod
from client.sdk import CliqueClient
from common.config import Config
from node.agent import NodeAgent
from scheduler.server import SchedulerServer


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_config(tmp_path, port: int, name: str, *, param_b: float = 7.0) -> Config:
    cfg = Config()
    cfg.node.display_name = name
    cfg.node.data_dir = tmp_path / name
    cfg.node.model_runtime = "echo"
    cfg.node.model_family = "echo"
    cfg.node.model_parameter_b = param_b
    cfg.node.heartbeat_interval_s = 0.2
    cfg.server.api_host = "127.0.0.1"
    cfg.server.api_port = port
    cfg.server.db_path = tmp_path / "server.db"
    cfg.server.lease_seconds = 30.0
    return cfg


@pytest_asyncio.fixture
async def clique(tmp_path):
    """Live server + 1 echo agent, with the MCP module wired to it."""
    port = free_port()
    server_cfg = make_config(tmp_path, port, "server-node")
    server = SchedulerServer(server_cfg)
    uv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1",
                                       port=port, log_level="warning"))
    server_task = asyncio.create_task(uv.serve())
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            try:
                await c.get(base + "/v1/clique")
                break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)

    cfg = make_config(tmp_path, port, "worker-node")
    agent = NodeAgent(cfg, base)
    agent_task = asyncio.create_task(agent.run())

    async with httpx.AsyncClient() as c:
        for _ in range(100):
            nodes = (await c.get(base + "/v1/nodes")).json()
            if any(n["status"] == "ready" for n in nodes):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"agent never ready: {nodes}")

    # Wire the MCP module to this server, isolated from ~/.clique state.
    sdk = CliqueClient(base)
    await sdk.authenticate(tmp_path / "mcp-client")
    mcp_mod._client = sdk

    yield base, server

    mcp_mod._client = None
    agent._stop.set()
    agent_task.cancel()
    uv.should_exit = True
    await asyncio.gather(agent_task, server_task, return_exceptions=True)


async def call(name: str, args: dict) -> dict | list:
    """Invoke one MCP tool and decode its payload."""
    result = await mcp_mod.mcp.call_tool(name, args)
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        # scalar/list returns are wrapped as {"result": ...}
        return sc["result"] if set(sc.keys()) == {"result"} else sc
    assert result.content, "tool returned no content"
    return json.loads(result.content[0].text)


async def test_tools_listed():
    tools = {t.name for t in await mcp_mod.mcp.list_tools()}
    assert {"clique_chat", "clique_submit", "clique_task_status",
            "clique_wait", "clique_cancel", "clique_code_task",
            "clique_session", "clique_status",
            "clique_models"} <= tools


async def test_chat_roundtrip(clique):
    out = await call("clique_chat", {"prompt": "hello clique"})
    assert out["state"] == "succeeded"
    assert out["output"]  # echo runtime returns something
    assert out["task_id"].startswith("t-") or out["task_id"]


async def test_submit_status_wait(clique):
    sub = await call("clique_submit", {"prompt": "async job"})
    task_id = sub["task_id"]
    assert task_id

    done = await call("clique_wait", {"task_id": task_id,
                                      "timeout_s": 20.0})
    assert done["state"] == "succeeded"
    assert done["output"]

    # terminal status includes the final output
    status = await call("clique_task_status", {"task_id": task_id})
    assert status["state"] == "succeeded"
    assert status["output"] == done["output"]


async def test_session_context(clique):
    sess = await call("clique_session", {})
    sid = sess["session_id"]
    assert sid.startswith("s-")
    out = await call("clique_chat", {"prompt": "turn one",
                                     "session_id": sid})
    assert out["state"] == "succeeded"


async def test_status_and_models(clique):
    status = await call("clique_status", {})
    assert any(n["status"] == "ready" for n in status["nodes"])
    assert "stats" in status

    models = await call("clique_models", {})
    assert "clique" in models
    assert any("echo" in m for m in models)


async def test_cancel_nonexistent_is_error(clique):
    # surfaced as a tool error (HTTP 404 from the server), not a crash
    from mcp.server.mcpserver.exceptions import UnexpectedToolError
    with pytest.raises(UnexpectedToolError):
        await mcp_mod.mcp.call_tool(
            "clique_cancel", {"task_id": "t-does-not-exist"})


async def test_entry_point_wired():
    import tomllib
    from pathlib import Path
    py = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert py["project"]["scripts"]["clique-mcp"] == \
        "client.mcp_server:main"


async def test_workspace_tools_listed():
    tools = {t.name for t in await mcp_mod.mcp.list_tools()}
    assert {"clique_workspace_create", "clique_workspace_list",
            "clique_workspace_read", "clique_workspace_write",
            "clique_workspace_patch", "clique_workspace_history",
            "clique_workspace_flush", "clique_workspace_task"} <= tools


async def test_workspace_shared_context_roundtrip(clique):
    """Two 'agents' (MCP writer + raw SDK reader) share one live workspace."""
    base, _server = clique
    ws = await call("clique_workspace_create",
                    {"files": {"main.py": "x = 1\n"}})
    wid = ws["workspace_id"]

    # agent A writes through MCP
    ev = await call("clique_workspace_write",
                    {"workspace_id": wid, "path": "main.py",
                     "text": "x = 2\n"})
    assert ev["seq"] == 2 and ev["version"] == 2

    # agent B (separate authenticated client) sees the write immediately
    other = CliqueClient(base)
    await other.authenticate()
    st = await other.workspace_file(wid, "main.py")
    assert st["text"] == "x = 2\n" and st["version"] == 2

    # agent B patches a line; agent A observes via history
    await other.workspace_patch(
        wid, "main.py", [{"op": "insert", "line": 2, "text": "y = 3\n"}],
        base_version=st["version"])
    hist = await call("clique_workspace_history", {"workspace_id": wid})
    assert [h["seq"] for h in hist] == [2, 3]  # seq 1 = create seed, not an op
    read = await call("clique_workspace_read",
                      {"workspace_id": wid, "path": "main.py"})
    assert read["text"] == "x = 2\ny = 3\n"

    # git checkpoint works
    flush = await call("clique_workspace_flush", {"workspace_id": wid})
    assert flush["sha"]
