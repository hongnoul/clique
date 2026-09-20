"""End-to-end integration tests: real uvicorn server + real agents over
HTTP/WS on localhost. Echo runtime, no model download needed.

Covers: registration (first-client-is-op), heartbeats, task lifecycle,
routing policy (longer prompts to bigger models), one-task-per-node,
node-loss requeue, idempotent submit, cancellation.
"""

from __future__ import annotations

import asyncio
import socket
import uuid

import httpx
import pytest
import pytest_asyncio
import uvicorn

from common.config import Config
from common.types import ModelSpec, NodeStatus, TaskRequest, TaskState
from node.agent import NodeAgent
from scheduler.server import SchedulerServer


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_config(tmp_path, port: int, name: str, *, param_b: float = 7.0,
                lease_seconds: float = 30.0) -> Config:
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
    cfg.server.lease_seconds = lease_seconds
    return cfg


@pytest_asyncio.fixture
async def clique(tmp_path):
    """Live server + 2 agents (7B and 70B echo models)."""
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

    agents, agent_tasks = [], []
    for name, size in (("small-node", 7.0), ("big-node", 70.0)):
        cfg = make_config(tmp_path, port, name, param_b=size)
        agent = NodeAgent(cfg, base)
        agents.append(agent)
        agent_tasks.append(asyncio.create_task(agent.run()))

    # wait until both agents are READY
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            nodes = (await c.get(base + "/v1/nodes")).json()
            ready = [n for n in nodes if n["status"] == "ready"]
            if len(ready) >= 2:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"agents never ready: {nodes}")

    yield base, server, agents

    for a in agents:
        a._stop.set()
    for t in agent_tasks:
        t.cancel()
    uv.should_exit = True
    await asyncio.gather(*agent_tasks, server_task, return_exceptions=True)


async def submit(base: str, prompt: str, **kw) -> str:
    req = TaskRequest(prompt=prompt, idempotency_key=uuid.uuid4().hex, **kw)
    async with httpx.AsyncClient() as c:
        r = await c.post(base + "/v1/tasks", json=req.model_dump(mode="json"))
        r.raise_for_status()
        return r.json()["task_id"]


async def wait_done(base: str, task_id: str, timeout: float = 15.0) -> dict:
    async with httpx.AsyncClient() as c:
        for _ in range(int(timeout / 0.1)):
            data = (await c.get(f"{base}/v1/tasks/{task_id}")).json()
            if data["state"] in ("succeeded", "failed", "cancelled", "expired"):
                return data
            await asyncio.sleep(0.1)
    raise TimeoutError(f"task {task_id}: {data['state']}")


# ---------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_join_and_first_client_is_op(clique):
    base, _, _ = clique
    async with httpx.AsyncClient() as c:
        nodes = (await c.get(base + "/v1/nodes")).json()
    by_name = {n["display_name"]: n for n in nodes}
    assert by_name["small-node"]["op_level"] == "op"      # first client = op
    assert by_name["big-node"]["op_level"] == "member"    # later = member
    assert all(n["status"] == "ready" for n in nodes)


@pytest.mark.asyncio
async def test_task_lifecycle_success(clique):
    base, _, _ = clique
    task_id = await submit(base, "hello clique world")
    data = await wait_done(base, task_id)
    assert data["state"] == "succeeded"
    assert "hello clique world" in data["result"]["output"]
    assert data["result"]["wall_time_s"] > 0
    assert data["assigned_node"]


@pytest.mark.asyncio
async def test_partial_output_streams_before_completion(clique):
    """partial_output is visible while the task runs, not just at done.

    Guards the live-streaming contract end to end: the agent flushes
    inference tokens as progress, the server accumulates them, and
    GET /v1/tasks/{id} exposes them before the terminal result lands.
    """
    base, _, _ = clique
    # long echo prompt: many words x 5ms each keeps the task alive long
    # enough to observe mid-flight progress deterministically.
    task_id = await submit(base, "word " * 200, max_output_tokens=200)
    seen_partial = ""
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            data = (await c.get(f"{base}/v1/tasks/{task_id}")).json()
            if data.get("partial_output"):
                seen_partial = data["partial_output"]
                break
            if data["state"] in ("succeeded", "failed", "cancelled",
                                 "expired"):
                break
            await asyncio.sleep(0.05)
    data = await wait_done(base, task_id)
    assert data["state"] == "succeeded"
    assert seen_partial, "no partial_output observed before completion"
    assert data["result"]["output"].startswith(seen_partial)


@pytest.mark.asyncio
async def test_long_prompt_routes_to_bigger_model(clique):
    base, server, _ = clique
    long_prompt = "word " * 3000  # ~3750 est tokens
    task_id = await submit(base, long_prompt, max_output_tokens=16)
    data = await wait_done(base, task_id)
    assert data["state"] == "succeeded"
    node = server.registry.get(data["assigned_node"])
    assert node.model.parameter_count_b == 70.0, \
        f"long prompt went to {node.model.parameter_count_b}B"


@pytest.mark.asyncio
async def test_short_prompt_avoids_big_model(clique):
    base, server, _ = clique
    task_id = await submit(base, "hi")
    data = await wait_done(base, task_id)
    node = server.registry.get(data["assigned_node"])
    assert node.model.parameter_count_b == 7.0, \
        f"short prompt went to {node.model.parameter_count_b}B"


@pytest.mark.asyncio
async def test_one_task_per_node_and_concurrency(clique):
    base, server, agents = clique
    # slow the echo down so tasks overlap
    for a in agents:
        a.runtime.delay_s = 0.05
    ids = [await submit(base, f"task number {i} some words here", max_output_tokens=64)
           for i in range(6)]
    # while running, no node may ever hold 2 active tasks
    for _ in range(50):
        active = server.router.list_tasks()
        nodes_busy = [t.assigned_node for t in active
                      if t.state in (TaskState.ASSIGNED, TaskState.RUNNING)
                      and t.assigned_node]
        assert len(nodes_busy) == len(set(nodes_busy)), "node holds 2 tasks"
        if all(t.state == TaskState.SUCCEEDED for t in active if t.request.task_id in ids):
            break
        await asyncio.sleep(0.1)
    results = [await wait_done(base, tid) for tid in ids]
    assert all(r["state"] == "succeeded" for r in results)
    used = {r["assigned_node"] for r in results}
    assert len(used) == 2, "work should spread across both nodes"


@pytest.mark.asyncio
async def test_idempotent_submit(clique):
    base, _, _ = clique
    key = uuid.uuid4().hex
    req = TaskRequest(prompt="idem", idempotency_key=key)
    async with httpx.AsyncClient() as c:
        r1 = (await c.post(base + "/v1/tasks", json=req.model_dump(mode="json"))).json()
        r2 = (await c.post(base + "/v1/tasks", json=req.model_dump(mode="json"))).json()
    assert r1["task_id"] == r2["task_id"]


@pytest.mark.asyncio
async def test_node_loss_requeues_task(clique):
    base, server, agents = clique
    small, big = agents
    # make execution slow enough to kill mid-flight
    small.runtime.delay_s = 0.2
    big.runtime.delay_s = 0.2
    task_id = await submit(base, "resilient " * 50, max_output_tokens=256)
    # wait until assigned
    assigned_node = None
    for _ in range(50):
        view = server.router.get_task(task_id)
        if view.assigned_node:
            assigned_node = view.assigned_node
            break
        await asyncio.sleep(0.1)
    assert assigned_node
    # kill the assigned agent abruptly (no drain, no leave message)
    victim = next(a for a in agents if a.node_id == assigned_node)
    await victim.kill()
    # server marks it offline after missed heartbeats and requeues
    data = await wait_done(base, task_id, timeout=20.0)
    assert data["state"] == "succeeded"
    assert data["assigned_node"] != assigned_node, "should finish on the other node"
    assert data["attempts"] >= 2


@pytest.mark.asyncio
async def test_cancel_queued_task(clique):
    base, server, agents = clique
    for a in agents:
        a.runtime.delay_s = 0.1
    # fill both nodes, then queue one more and cancel it
    blockers = [await submit(base, "block " * 30, max_output_tokens=64)
                for _ in range(2)]
    victim_id = await submit(base, "cancel me")
    async with httpx.AsyncClient() as c:
        r = (await c.post(f"{base}/v1/tasks/{victim_id}/cancel")).json()
    assert r["cancelled"] is True
    data = await wait_done(base, victim_id)
    assert data["state"] == "cancelled"
    for b in blockers:
        assert (await wait_done(base, b))["state"] == "succeeded"


@pytest.mark.asyncio
async def test_clusters_view(clique):
    base, _, _ = clique
    async with httpx.AsyncClient() as c:
        clusters = (await c.get(base + "/v1/clusters")).json()
        info = (await c.get(base + "/v1/clique")).json()
    keys = {c["cluster_key"] for c in clusters}
    assert "echo-7b-none" in keys and "echo-70b-none" in keys
    assert {c["cluster_key"] for c in info["clusters"]} == keys


@pytest.mark.asyncio
async def test_sdk_stream_yields_deltas_then_done(clique):
    """CliqueClient.stream() reassembles partial_output incrementally."""
    from client.sdk import CliqueClient
    from common.types import TaskState as _TS

    base, _, _ = clique
    client = CliqueClient(base)
    task_id = await client.submit("word " * 200, max_output_tokens=200)
    deltas: list[str] = []
    done = None
    async for delta, view in client.stream(task_id, poll_s=0.05,
                                           timeout_s=15.0):
        if delta:
            deltas.append(delta)
        if view is not None:
            done = view
    assert done is not None and done.state == _TS.SUCCEEDED
    assert deltas, "stream() yielded no progress deltas"
    assert "".join(deltas) == (done.result.output or "")


@pytest.mark.asyncio
async def test_sdk_stream_ws_push_no_polling(clique):
    """stream_ws() gets deltas over /ws/tasks/{id} without HTTP polling."""
    import json as _json

    import websockets as _ws

    from client.sdk import CliqueClient
    from common.types import TaskState as _TS

    base, _, _ = clique
    client = CliqueClient(base)
    # part 1: raw WS sees live progress events mid-flight
    watch_id = await client.submit("word " * 200, max_output_tokens=200)
    ws_url = base.replace("http", "ws", 1) + f"/ws/tasks/{watch_id}"
    events: list[str] = []
    async with _ws.connect(ws_url) as sock:
        deadline = asyncio.get_event_loop().time() + 15
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(sock.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            events.append(_json.loads(raw)["event"])
            if "task.finished" in events:
                break
    assert "task.progress" in events, f"no progress events: {events[:5]}"
    # part 2: SDK consumer on a fresh task reassembles the same output
    task_id = await client.submit("word " * 200, max_output_tokens=200)
    deltas: list[str] = []
    done = None
    async for delta, view in client.stream_ws(task_id, timeout_s=15.0):
        if delta:
            deltas.append(delta)
        if view is not None:
            done = view
    assert done is not None and done.state == _TS.SUCCEEDED
    assert deltas, "stream_ws() yielded no progress deltas"
    assert "".join(deltas) == (done.result.output or "")
