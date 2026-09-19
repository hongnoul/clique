"""Tests for the extended (P2/P3) surface: sessions + shared context,
permissions (/op), cron, vcs snapshots, suggestions, kick, /ws/events,
and the OpenAI-compatible /v1/chat/completions adapter.

Reuses the live-server ``clique`` fixture style from test_integration:
real uvicorn + real agents over HTTP/WS, echo runtime.
"""

from __future__ import annotations

import asyncio
import json
import socket
import uuid

import httpx
import pytest
import pytest_asyncio
import uvicorn
import websockets

from common.config import Config
from common.types import TaskRequest
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

    async with httpx.AsyncClient() as c:
        for _ in range(100):
            nodes = (await c.get(base + "/v1/nodes")).json()
            if len([n for n in nodes if n["status"] == "ready"]) >= 2:
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


def agent_by_name(server, agents, name: str) -> NodeAgent:
    for a in agents:
        info = server.registry.get(a.node_id)
        if info and info.display_name == name:
            return a
    raise KeyError(name)


def token_of(server, node_id: str) -> str:
    for token, nid in server.tokens.items():
        if nid == node_id:
            return token
    raise KeyError(node_id)


def hdr(server, agents, name: str) -> dict:
    a = agent_by_name(server, agents, name)
    return {"Authorization": f"Bearer {token_of(server, a.node_id)}"}


async def wait_done(base: str, task_id: str, timeout: float = 15.0) -> dict:
    async with httpx.AsyncClient() as c:
        for _ in range(int(timeout / 0.1)):
            data = (await c.get(f"{base}/v1/tasks/{task_id}")).json()
            if data["state"] in ("succeeded", "failed", "cancelled", "expired"):
                return data
            await asyncio.sleep(0.1)
    raise TimeoutError(f"task {task_id}: {data['state']}")


# ------------------------------------------------------------------- sessions


@pytest.mark.asyncio
async def test_session_lifecycle_and_context(clique):
    base, server, agents = clique
    op_hdr = hdr(server, agents, "small-node")
    async with httpx.AsyncClient() as c:
        r = await c.post(base + "/v1/sessions",
                         json={"cluster_key": "echo-7b-none"}, headers=op_hdr)
        r.raise_for_status()
        session = r.json()
        sid = session["session_id"]

        # a task in the session appends the user turn and the reply
        req = TaskRequest(prompt="hello session", session_id=sid,
                          idempotency_key=uuid.uuid4().hex)
        tid = (await c.post(base + "/v1/tasks",
                            json=req.model_dump(mode="json"))).json()["task_id"]
        data = await wait_done(base, tid)
        assert data["state"] == "succeeded"

        detail = (await c.get(f"{base}/v1/sessions/{sid}")).json()
        turns = json.loads(detail["context"])
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert detail["context_version"] == 2
        assert detail["pinned_node"]  # pinned to whoever answered

        # session task routed inside its cluster (7B, cluster_key hint)
        node = server.registry.get(data["assigned_node"])
        assert node.model.cluster_key() == "echo-7b-none"

        # close
        r = await c.delete(f"{base}/v1/sessions/{sid}", headers=op_hdr)
        assert r.json()["closed"]
        active = (await c.get(base + "/v1/sessions")).json()
        assert sid not in [s["session_id"] for s in active]


@pytest.mark.asyncio
async def test_session_migration(clique):
    base, server, agents = clique
    op_hdr = hdr(server, agents, "small-node")
    small = agent_by_name(server, agents, "small-node")
    big = agent_by_name(server, agents, "big-node")
    async with httpx.AsyncClient() as c:
        sid = (await c.post(base + "/v1/sessions",
                            json={"cluster_key": "echo-7b-none"},
                            headers=op_hdr)).json()["session_id"]
        # migration to a node outside the cluster is refused
        r = await c.post(f"{base}/v1/sessions/{sid}/migrate",
                         json={"to_node": big.node_id}, headers=op_hdr)
        assert r.status_code == 409
        # migration inside the cluster works
        r = await c.post(f"{base}/v1/sessions/{sid}/migrate",
                         json={"to_node": small.node_id}, headers=op_hdr)
        assert r.status_code == 200
        assert r.json()["pinned_node"] == small.node_id


# ---------------------------------------------------------------- permissions


@pytest.mark.asyncio
async def test_op_grant_revoke_and_gating(clique):
    base, server, agents = clique
    op_hdr = hdr(server, agents, "small-node")      # first client = op
    member_hdr = hdr(server, agents, "big-node")    # later = member
    big = agent_by_name(server, agents, "big-node")
    async with httpx.AsyncClient() as c:
        # member may not op anyone
        r = await c.post(base + "/v1/permissions/op",
                         json={"target": big.node_id}, headers=member_hdr)
        assert r.status_code == 403
        # op grants op to member
        r = await c.post(base + "/v1/permissions/op",
                         json={"target": big.node_id}, headers=op_hdr)
        assert r.status_code == 200
        levels = {p["display_name"]: p["level"]
                  for p in (await c.get(base + "/v1/permissions")).json()}
        assert levels["big-node"] == "op"
        # deop again
        r = await c.post(base + "/v1/permissions/deop",
                         json={"target": big.node_id}, headers=op_hdr)
        assert r.status_code == 200
        # audit log recorded both
        audit = (await c.get(base + "/v1/permissions/audit")).json()
        actions = [e["action"] for e in audit]
        assert "grant_op" in actions and "revoke_op" in actions
        # unauthenticated requests are rejected
        r = await c.post(base + "/v1/permissions/op",
                         json={"target": big.node_id})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_kick_requires_op_and_removes_node(clique):
    base, server, agents = clique
    member_hdr = hdr(server, agents, "big-node")
    op_hdr = hdr(server, agents, "small-node")
    big = agent_by_name(server, agents, "big-node")
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{base}/v1/nodes/{big.node_id}/kick",
                         headers=member_hdr)
        assert r.status_code == 403
        r = await c.post(f"{base}/v1/nodes/{big.node_id}/kick", headers=op_hdr)
        assert r.status_code == 200
        info = server.registry.get(big.node_id)
        assert info.status.value == "offline"


# ----------------------------------------------------------------------- cron


@pytest.mark.asyncio
async def test_cron_request_approve_fire(clique):
    base, server, agents = clique
    op_hdr = hdr(server, agents, "small-node")
    member_hdr = hdr(server, agents, "big-node")
    async with httpx.AsyncClient() as c:
        template = TaskRequest(prompt="scheduled hello",
                               idempotency_key="template").model_dump(mode="json")
        # invalid expression rejected
        r = await c.post(base + "/v1/cron", headers=member_hdr,
                         json={"cron_expr": "not a cron", "task_template": template})
        assert r.status_code == 422
        # member may request
        r = await c.post(base + "/v1/cron", headers=member_hdr,
                         json={"cron_expr": "* * * * *", "task_template": template})
        job = r.json()
        assert job["approved_by"] is None and not job["enabled"]
        # member may not approve
        r = await c.post(f"{base}/v1/cron/{job['cron_id']}/approve",
                         headers=member_hdr)
        assert r.status_code == 403
        # op approves -> enabled
        r = await c.post(f"{base}/v1/cron/{job['cron_id']}/approve",
                         headers=op_hdr)
        assert r.json()["enabled"] is True
    # manual fire submits a task; refire for the same slot is idempotent
    tid = server.cron.fire(job["cron_id"])
    data = await wait_done(base, tid)
    assert data["state"] == "succeeded"
    assert "scheduled hello" in data["result"]["output"]
    # requester can disable
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{base}/v1/cron/{job['cron_id']}/disable",
                         headers=member_hdr)
        assert r.status_code == 200
        jobs = (await c.get(base + "/v1/cron")).json()
        assert jobs[0]["enabled"] is False


# ------------------------------------------------------------------------ vcs


@pytest.mark.asyncio
async def test_vcs_snapshot_history_diff_rollback(clique):
    base, server, agents = clique
    op_hdr = hdr(server, agents, "small-node")
    member_hdr = hdr(server, agents, "big-node")
    big = agent_by_name(server, agents, "big-node")
    async with httpx.AsyncClient() as c:
        # baseline exists from init; op changes trigger snapshots
        await c.post(base + "/v1/permissions/op",
                     json={"target": big.node_id}, headers=op_hdr)
        await c.post(base + "/v1/permissions/deop",
                     json={"target": big.node_id}, headers=op_hdr)
        history = (await c.get(base + "/v1/vcs/history")).json()
        assert len(history) >= 3
        grant_sha, base_sha = history[1]["sha"], history[-1]["sha"]
        diff = (await c.get(base + "/v1/vcs/diff",
                            params={"a": base_sha, "b": grant_sha})).json()["diff"]
        assert '"level": "op"' in diff  # big-node became op in permissions.json
        # member cannot rollback
        r = await c.post(base + "/v1/vcs/rollback", json={"sha": base_sha},
                         headers=member_hdr)
        assert r.status_code == 403
        # op rollback adds a new commit (history never rewritten)
        r = await c.post(base + "/v1/vcs/rollback", json={"sha": base_sha},
                         headers=op_hdr)
        assert r.status_code == 200
        history2 = (await c.get(base + "/v1/vcs/history")).json()
        assert len(history2) > len(history)  # history never rewritten
        assert any(h["message"].startswith("rollback to") for h in history2)


# ---------------------------------------------------------------- suggestions


@pytest.mark.asyncio
async def test_suggestion_engine_overload(tmp_path):
    """Deterministic unit path: queued hinted tasks + one idle node in
    another cluster -> swap_model suggestion; dismiss suppresses."""
    from datetime import timedelta

    from common.types import ModelSpec, ResourceSnapshot
    from scheduler import suggestions as sg
    from scheduler.registry import Registry
    from scheduler.router import Router

    registry = Registry(tmp_path / "unit.db")
    seven = ModelSpec(family="echo", parameter_count_b=7.0)
    seventy = ModelSpec(family="echo", parameter_count_b=70.0)
    res = ResourceSnapshot(memory_available_mb=200_000)
    registry.register(node_id="n-small", display_name="small", public_key="",
                      address="", model=seven, resources=res)
    registry.register(node_id="n-big", display_name="big", public_key="",
                      address="", model=seventy, resources=res)
    router = Router(registry, tmp_path / "unit.db")
    for i in range(5):
        router.submit(TaskRequest(prompt=f"queued {i}",
                                  model_hint="echo-7b-none",
                                  idempotency_key=f"k{i}"))

    engine = sg.SuggestionEngine(registry, router)
    old_window = sg.OBSERVE_WINDOW
    sg.OBSERVE_WINDOW = timedelta(seconds=0)
    try:
        engine.analyze()
        engine.analyze()
        active = engine.active()
        assert active, "expected a swap_model suggestion"
        s = active[0]
        assert s.kind == "swap_model"
        assert s.to_cluster == "echo-7b-none"
        assert s.target_node == "n-big"
        # dismiss suppresses re-emission of the identical suggestion
        engine.dismiss(s.suggestion_id, "tester")
        engine.analyze()
        assert all(x.suggestion_id != s.suggestion_id
                   for x in engine.active())
        report = engine.overload_report()
        assert report["chat"]["queued"] == 5
    finally:
        sg.OBSERVE_WINDOW = old_window


# ------------------------------------------------------------------ ws events


@pytest.mark.asyncio
async def test_ws_events_firehose(clique):
    base, server, agents = clique
    ws_url = base.replace("http", "ws", 1) + "/ws/events"
    got: list[dict] = []
    async with websockets.connect(ws_url) as ws:
        async with httpx.AsyncClient() as c:
            req = TaskRequest(prompt="event test",
                              idempotency_key=uuid.uuid4().hex)
            await c.post(base + "/v1/tasks", json=req.model_dump(mode="json"))
        deadline = asyncio.get_event_loop().time() + 10
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            got.append(json.loads(raw))
            events = {m["event"] for m in got}
            if {"task.submitted", "task.assigned", "task.finished"} <= events:
                break
    events = {m["event"] for m in got}
    assert {"task.submitted", "task.assigned", "task.finished"} <= events


# ------------------------------------------------------- chat completions API


@pytest.mark.asyncio
async def test_openai_compat_chat_completions(clique):
    base, _, _ = clique
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(base + "/v1/chat/completions", json={
            "model": "clique",
            "messages": [{"role": "user", "content": "compat hello"}],
        })
        r.raise_for_status()
        data = r.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert "compat hello" in data["choices"][0]["message"]["content"]
    assert data["usage"]["total_tokens"] > 0


@pytest.mark.asyncio
async def test_openai_compat_streaming(clique):
    base, _, _ = clique
    chunks: list[str] = []
    async with httpx.AsyncClient(timeout=30.0) as c:
        async with c.stream("POST", base + "/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "stream me"}],
            "stream": True,
        }) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                delta = chunk["choices"][0]["delta"]
                if "content" in delta:
                    chunks.append(delta["content"])
    assert "stream me" in "".join(chunks)


# -------------------------------------------------------------------- web dash


@pytest.mark.asyncio
async def test_web_dashboard_served(clique):
    base, _, _ = clique
    async with httpx.AsyncClient() as c:
        r = await c.get(base + "/dash")
    assert r.status_code == 200
    assert "clique dashboard" in r.text
