"""Tests for the extended (P2/P3) surface: sessions + shared context,
vcs snapshots, suggestions, kick, /ws/events,
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

        # next turn replays server-held history onto the worker prompt
        req2 = TaskRequest(prompt="follow-up turn", session_id=sid,
                           idempotency_key=uuid.uuid4().hex)
        tid2 = (await c.post(base + "/v1/tasks",
                             json=req2.model_dump(mode="json"))).json()["task_id"]
        data2 = await wait_done(base, tid2)
        assert data2["state"] == "succeeded"
        assert "hello session" in data2["result"]["output"]
        assert "follow-up turn" in data2["result"]["output"]
        assert data2["assigned_node"]  # hop allowed; any 7B replica is fine
        node2 = server.registry.get(data2["assigned_node"])
        assert node2.model.cluster_key() == "echo-7b-none"

        # close
        r = await c.delete(f"{base}/v1/sessions/{sid}", headers=op_hdr)
        assert r.json()["closed"]
        active = (await c.get(base + "/v1/sessions")).json()
        assert sid not in [s["session_id"] for s in active]


@pytest.mark.asyncio
async def test_sticky_cli_session_remembers_turns(clique, tmp_path):
    """Consecutive submits through ensure_chat_session replay history."""
    base, _, _ = clique
    from client.sdk import CliqueClient
    store = tmp_path / "cli-sessions.json"
    client = CliqueClient(base)
    await client.authenticate(data_dir=tmp_path / "cli-id")
    sid = await client.ensure_chat_session("echo-7b-none", store_path=store)
    again = await client.ensure_chat_session("echo-7b-none", store_path=store)
    assert again == sid

    tid1 = await client.submit("remember my favorite color is red",
                               session_id=sid)
    data1 = await wait_done(base, tid1)
    assert data1["state"] == "succeeded"

    tid2 = await client.submit("what is my favorite color", session_id=sid)
    data2 = await wait_done(base, tid2)
    assert data2["state"] == "succeeded"
    assert "favorite color is red" in data2["result"]["output"]

    fresh = await client.ensure_chat_session("echo-7b-none", store_path=store,
                                             reset=True)
    assert fresh != sid


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




@pytest.mark.asyncio
async def test_kick_requires_auth_and_removes_node(clique):
    base, server, agents = clique
    op_hdr = hdr(server, agents, "small-node")
    big = agent_by_name(server, agents, "big-node")
    async with httpx.AsyncClient() as c:
        # unauthenticated kick is rejected
        r = await c.post(f"{base}/v1/nodes/{big.node_id}/kick")
        assert r.status_code == 401
        r = await c.post(f"{base}/v1/nodes/{big.node_id}/kick", headers=op_hdr)
        assert r.status_code == 200
        info = server.registry.get(big.node_id)
        # idle (no task in flight) -> zero reap grace, so it may already be
        # gone by the time we check, not just marked offline
        assert info is None or info.status.value == "offline"




# ------------------------------------------------------------------------ vcs


@pytest.mark.asyncio
async def test_vcs_snapshot_history_diff_rollback(clique):
    base, server, agents = clique
    any_hdr = hdr(server, agents, "small-node")
    async with httpx.AsyncClient() as c:
        # baseline exists from init; take an explicit snapshot
        r = await c.post(base + "/v1/vcs/snapshot",
                         json={"message": "test snapshot"}, headers=any_hdr)
        assert r.status_code == 200
        history = (await c.get(base + "/v1/vcs/history")).json()
        assert len(history) >= 1
        base_sha = history[-1]["sha"]
        if len(history) >= 2:
            diff = (await c.get(base + "/v1/vcs/diff",
                                params={"a": base_sha,
                                        "b": history[0]["sha"]})).json()
            assert "diff" in diff
        # unauthenticated rollback is rejected
        r = await c.post(base + "/v1/vcs/rollback", json={"sha": base_sha})
        assert r.status_code == 401
        # authenticated rollback adds a new commit (history never rewritten)
        r = await c.post(base + "/v1/vcs/rollback", json={"sha": base_sha},
                         headers=any_hdr)
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


@pytest.mark.asyncio
async def test_openai_compat_reasoning_split_nonstream(clique):
    """A complete output containing ``</think>`` is split: content is
    clean, chain-of-thought moves to message.reasoning_content."""
    base, _, _ = clique
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(base + "/v1/chat/completions", json={
            "model": "clique",
            "messages": [{"role": "user",
                          "content": "ponder</think> final-answer"}],
        })
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
    assert "</think>" not in msg["content"]
    assert "final-answer" in msg["content"]
    assert "ponder" in msg.get("reasoning_content", "")


@pytest.mark.asyncio
async def test_openai_compat_streaming_contract(clique):
    """Streaming: task-id header present, think tags never leak into
    content deltas, terminal chunk carries a finish_reason."""
    base, _, _ = clique
    content, reasoning, finish = [], [], None
    async with httpx.AsyncClient(timeout=30.0) as c:
        async with c.stream("POST", base + "/v1/chat/completions", json={
            "messages": [{"role": "user",
                          "content": "mull</think> streamed-answer"}],
            "stream": True,
        }) as r:
            r.raise_for_status()
            assert r.headers.get("x-clique-task-id", "").startswith("t-")
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                choice = json.loads(payload)["choices"][0]
                delta = choice["delta"]
                content.append(delta.get("content", ""))
                reasoning.append(delta.get("reasoning_content", ""))
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
    joined_content = "".join(content)
    assert finish in ("stop", "length")
    assert "</think>" not in joined_content
    assert "<think>" not in joined_content
    assert "streamed-answer" in joined_content + "".join(reasoning)


@pytest.mark.asyncio
async def test_openai_compat_models_endpoint(clique):
    base, _, _ = clique
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(base + "/v1/models")
        r.raise_for_status()
        data = r.json()
    assert data["object"] == "list"
    ids = [m["id"] for m in data["data"]]
    assert "clique" in ids
    assert any("echo" in mid for mid in ids)


# -------------------------------------------------------------------- web dash


@pytest.mark.asyncio
async def test_web_dashboard_served(clique):
    base, _, _ = clique
    async with httpx.AsyncClient() as c:
        r = await c.get(base + "/dash")
        assets = await c.get(base + "/assets/base.css")
        chrome = await c.get(base + "/assets/chrome.js")
    assert r.status_code == 200
    assert "clique dashboard" in r.text
    # click-to-watch live output must be wired, not orphan markup
    for needle in ("watchTask", "/ws/tasks/", "liveOut", "stopWatch"):
        assert needle in r.text, f"dashboard missing {needle}"
    # shared chrome is linked, not inlined per page
    assert assets.status_code == 200 and "--accent" in assets.text
    assert chrome.status_code == 200 and "applyTheme" in chrome.text
    assert "/chat" in r.text  # menu links to the chat page (notes stub gone)
    assert "Notes" not in r.text


@pytest.mark.asyncio
async def test_web_chat_page_served(clique):
    base, _, _ = clique
    async with httpx.AsyncClient() as c:
        r = await c.get(base + "/chat")
    assert r.status_code == 200
    assert "clique chat" in r.text
    # the page must drive the same path as `clique submit`
    for needle in ("/v1/chat/sessions", "/v1/tasks", "/ws/tasks/",
                   "/ws/sessions/", "idempotency_key"):
        assert needle in r.text, f"chat page missing {needle}"


@pytest.mark.asyncio
async def test_browser_chat_session_roundtrip(clique):
    """The web chat opens/closes sessions without a node keypair, and a
    turn through one lands in the transcript the page renders."""
    base, _, _ = clique
    async with httpx.AsyncClient() as c:
        sid = (await c.post(base + "/v1/chat/sessions",
                            json={"cluster_key": "echo-7b-none"})
               ).json()["session_id"]
        assert sid.startswith("s-")

        req = TaskRequest(prompt="hi from the browser", session_id=sid,
                          idempotency_key=uuid.uuid4().hex)
        tid = (await c.post(base + "/v1/tasks",
                            json=req.model_dump(mode="json"))).json()["task_id"]
        assert (await wait_done(base, tid))["state"] == "succeeded"

        detail = (await c.get(f"{base}/v1/sessions/{sid}")).json()
        assert [t["role"] for t in json.loads(detail["context"])] == \
            ["user", "assistant"]

        assert (await c.delete(f"{base}/v1/chat/sessions/{sid}")).json()["closed"]
        active = (await c.get(base + "/v1/sessions")).json()
        assert sid not in [s["session_id"] for s in active]
        r = await c.delete(f"{base}/v1/chat/sessions/s-nope")
        assert r.status_code == 404


# -------------------------------------------------------------------- sdk auth


@pytest.mark.asyncio
async def test_sdk_silent_reauth_on_stale_token(clique, tmp_path):
    """SDK retries once with fresh registration when the server has
    forgotten its token (in-memory tokens die on server restart)."""
    from client.sdk import CliqueClient

    base, server, _ = clique
    client = CliqueClient(base)
    await client.authenticate(tmp_path / "sdk-test")
    assert client.token is not None
    # sanity: authed call works (sessions route is token-gated)
    first = await client.create_session()
    assert first["session_id"].startswith("s-")

    # simulate server restart: wipe in-memory tokens
    server.tokens.clear()
    # caller never sees the 401: SDK re-registers and retries silently
    second = await client.create_session()
    assert second["session_id"].startswith("s-")
    assert client.token is not None


# -------------------------------------------------------- vcs nested trees


def test_vcs_nested_tree_diff_and_rollback(tmp_path):
    """code-repo commits are nested (<task_id>/<path>); diff, history
    walking, and rollback must traverse subtrees, not just the root
    tree (regression: 'Tree' object has no attribute 'data')."""
    from scheduler.vcs import VcsService

    vcs = VcsService(tmp_path / "code-repo")
    vcs.init()
    deep = vcs.repo_dir / "task-1" / "pkg" / "mod.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("x = 1\n")
    sha1 = vcs.commit_paths(["task-1/pkg/mod.py"], "seed", "n1")
    assert sha1

    # no-op commit is skipped even for nested paths
    assert vcs.commit_paths(["task-1/pkg/mod.py"], "noop", "n1") is None

    deep.write_text("x = 2\n")
    sha2 = vcs.commit_paths(["task-1/pkg/mod.py"], "bump", "n1")
    diff = vcs.diff(sha1, sha2)
    assert "task-1/pkg/mod.py" in diff and "+x = 2" in diff

    # rollback restores nested files (and creates parent dirs)
    import shutil
    shutil.rmtree(vcs.repo_dir / "task-1")
    vcs.rollback(sha1, "n1")
    assert deep.read_text() == "x = 1\n"
    assert len(vcs.history(limit=10)) == 4  # baseline+seed+bump+rollback
