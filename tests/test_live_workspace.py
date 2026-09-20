"""Live workspace realtime collab tests.

Covers the two-layer design:
- sequenced in-memory patches (concurrent writers linearize)
- stale-base rebase (line shift)
- WS fanout to all viewers + sync recovery + presence
- debounced git checkpoint behind the live layer
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from common.config import Config
from common.types import TaskAssignment, TaskRequest
from scheduler.server import SchedulerServer
from scheduler.workspace import WorkspaceService


def make_server(tmp_path) -> SchedulerServer:
    cfg = Config()
    cfg.node.display_name = "test-server"
    cfg.node.data_dir = tmp_path
    cfg.server.db_path = tmp_path / "server.db"
    return SchedulerServer(cfg)


# -- unit: sequencer ----------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_patches_linearize(tmp_path):
    svc = WorkspaceService(tmp_path)
    svc.create("w", {"m.py": "a\nb\nc\n"})
    e1, e2 = await asyncio.gather(
        svc.apply_patch("w", "m.py", 1,
                        [{"op": "insert", "line": 1, "text": "A\n"}], "h"),
        svc.apply_patch("w", "m.py", 1,
                        [{"op": "insert", "line": 3, "text": "B\n"}], "a"),
    )
    assert {e1["seq"], e2["seq"]} == {2, 3}  # linear, no fork
    assert e1["rebased"] is False  # first writer fast path
    assert e2["rebased"] is True  # second writer rebased
    snap = svc.snapshot("w")
    assert snap["seq"] == 3
    assert snap["files"]["m.py"]["version"] == 3


@pytest.mark.asyncio
async def test_stale_delete_rebased_over_insert(tmp_path):
    svc = WorkspaceService(tmp_path)
    svc.create("w", {"m.py": "a\nb\nc\n"})
    # writer 1 inserts at top (v1->v2)
    await svc.apply_patch("w", "m.py", 1,
                          [{"op": "insert", "line": 1, "text": "TOP\n"}], "h")
    # writer 2 deletes old line 1 with stale base 1 -> rebased below insert
    e = await svc.apply_patch("w", "m.py", 1,
                              [{"op": "delete", "line": 1, "count": 1}], "a")
    assert e["rebased"] is True
    text = svc.file_state("w", "m.py")["text"]
    assert "TOP" in text  # insert survives
    assert text.count("a\n") == 0  # stale delete still removed its target


@pytest.mark.asyncio
async def test_debounced_git_checkpoint(tmp_path):
    svc = WorkspaceService(tmp_path)
    svc.FLUSH_IDLE_S = 0.05
    svc.create("w", {"m.py": "a\n"})
    await svc.apply_patch("w", "m.py", 1,
                          [{"op": "insert", "line": 2, "text": "b\n"}], "h")
    await asyncio.sleep(0.2)  # let debounce fire
    from dulwich.repo import Repo
    repo = Repo(str(tmp_path / "w"))
    msgs = [c.commit.message.decode() for c in repo.get_walker(max_entries=5)]
    assert any("live checkpoint" in m for m in msgs)


# -- e2e: WS ------------------------------------------------------------


def test_ws_fanout_rebase_sync_presence(tmp_path):
    server = make_server(tmp_path)
    client = TestClient(server.app)
    server.tokens["t1"] = "n1"
    server.tokens["t2"] = "n2"
    server.live_workspaces.create("w1", {"a.py": "1\n2\n3\n"})

    with client.websocket_connect("/ws/workspace/w1?token=t1") as a, \
            client.websocket_connect("/ws/workspace/w1?token=t2") as b:
        a.receive_text()
        b.receive_text()
        a.send_text(json.dumps({
            "type": "workspace.patch", "path": "a.py", "base_version": 1,
            "ops": [{"op": "insert", "line": 1, "text": "A\n"}]}))
        da = json.loads(a.receive_text())
        db = json.loads(b.receive_text())
        assert da["seq"] == db["seq"] == 2  # both viewers same seq

        b.send_text(json.dumps({
            "type": "workspace.patch", "path": "a.py", "base_version": 1,
            "ops": [{"op": "insert", "line": 4, "text": "B\n"}]}))
        da2 = json.loads(a.receive_text())
        db2 = json.loads(b.receive_text())
        assert db2["rebased"] is True

        a.send_text(json.dumps({"type": "workspace.sync", "path": "a.py"}))
        st = json.loads(a.receive_text())
        assert st["type"] == "workspace.state"
        assert "A\n" in st["text"] and "B\n" in st["text"]

        a.send_text(json.dumps(
            {"type": "workspace.presence", "path": "a.py", "line": 3}))
        p = json.loads(b.receive_text())
        assert p["payload"]["line"] == 3 if "payload" in p else p["line"] == 3


def test_rest_workspace_crud(tmp_path):
    server = make_server(tmp_path)
    client = TestClient(server.app)
    server.live_workspaces.create("w1", {"a.py": "x\n"})
    assert client.get("/v1/workspaces").status_code == 200
    r = client.get("/v1/workspaces/w1")
    assert r.status_code == 200 and r.json()["seq"] == 1
    r = client.get("/v1/workspaces/w1/file", params={"path": "a.py"})
    assert r.status_code == 200 and r.json()["text"] == "x\n"


@pytest.mark.asyncio
async def test_rehydrate_after_restart(tmp_path):
    svc = WorkspaceService(tmp_path)
    svc.FLUSH_IDLE_S = 0.01
    svc.create("w", {"m.py": "hello\n"})
    await svc.apply_patch("w", "m.py", 1,
                          [{"op": "insert", "line": 2, "text": "world\n"}], "h")
    await svc.force_flush("w")
    # fresh service over same dir (simulates server restart)
    svc2 = WorkspaceService(tmp_path)
    assert "w" in svc2.list_ids()
    st = svc2.file_state("w", "m.py")
    assert "hello" in st["text"] and "world" in st["text"]
    assert st["version"] == 1  # history restarts, git holds the past


@pytest.mark.asyncio
async def test_path_guards(tmp_path):
    svc = WorkspaceService(tmp_path)
    with pytest.raises(ValueError):
        svc.create("w", {"../evil.py": "x"})  # traversal
    with pytest.raises(ValueError):
        svc.create("w", {"/abs.py": "x"})  # absolute
    svc.create("w", {"ok.py": "x\n"})
    with pytest.raises(ValueError):
        await svc.apply_patch("w", "../evil.py", 1,
                              [{"op": "insert", "line": 1, "text": "x\n"}], "h")
    with pytest.raises(ValueError):
        await svc.apply_patch("w", "ok.py", 1,
                              [{"op": "insert", "line": 1, "text": "x\n"}] * 101,
                              "h")  # op budget


@pytest.mark.asyncio
async def test_task_stamps_workspace_seq(tmp_path):
    server = make_server(tmp_path)
    server.live_workspaces.create("w1", {"a.py": "x\n"})
    req = TaskRequest(prompt="do work", idempotency_key="k1",
                      workspace_id="w1")
    tid = await server.submit_task(req)
    view = server.router.get_task(tid)
    assert view.request.workspace_seq == 1
    # unknown workspace rejected
    bad = TaskRequest(prompt="x", idempotency_key="k2",
                      workspace_id="nope")
    with pytest.raises(Exception):
        await server.submit_task(bad)


@pytest.mark.asyncio
async def test_executor_drift_note():
    from datetime import datetime, timezone

    from node.executor import execute

    class FakeRuntime:
        def __init__(self):
            self.seen: list[str] = []

        async def infer_stream(self, prompt, max_tokens):
            self.seen.append(prompt)
            yield '{"op": "done"}\n'

    req = TaskRequest(prompt="TOOLS: fix\n--- file: m.py ---\nx\n",
                      idempotency_key="k")
    asg = TaskAssignment(task_id="t", attempt_id="a", node_id="n",
                         lease_expires_at=datetime.now(timezone.utc),
                         reason="r")
    rt = FakeRuntime()
    await execute(None, rt, asg, req,
                  workspace_drift=lambda: ["m.py"])
    assert "live workspace changed" in rt.seen[-1]
    assert "m.py" in rt.seen[-1]
    rt2 = FakeRuntime()
    await execute(None, rt2, asg, req, workspace_drift=lambda: [])
    assert "live workspace changed" not in rt2.seen[-1]


def test_history_and_commits_routes(tmp_path):
    server = make_server(tmp_path)
    client = TestClient(server.app)
    server.live_workspaces.create("w1", {"a.py": "1\n"})
    asyncio.run(server.apply_workspace_patch(
        "w1", "a.py", 1, [{"op": "insert", "line": 2, "text": "2\n"}], "n1"))
    r = client.get("/v1/workspaces/w1/history")
    assert r.status_code == 200 and len(r.json()) == 1
    assert r.json()[0]["actor"] == "n1"
    r = client.get("/v1/workspaces/w1/history", params={"path": "a.py"})
    assert r.status_code == 200
    r = client.get("/v1/workspaces/w1/history", params={"path": "nope.py"})
    assert r.status_code == 200 and r.json() == []
    # force flush then commits visible
    asyncio.run(server.live_workspaces.force_flush("w1"))
    r = client.get("/v1/workspaces/w1/commits")
    assert r.status_code == 200 and len(r.json()) >= 2
    r = client.get("/v1/workspaces/nope/history")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_linked_task_result_flushes_workspace(tmp_path):
    server = make_server(tmp_path)
    server.live_workspaces.FLUSH_IDLE_S = 3600  # disable debounce
    server.live_workspaces.create("w1", {"a.py": "x\n"})
    await server.apply_workspace_patch(
        "w1", "a.py", 1, [{"op": "insert", "line": 2, "text": "y\n"}], "n1")
    ws = server.live_workspaces.get("w1")
    assert ws.dirty is True
    tid = await server.submit_task(
        TaskRequest(prompt="w", idempotency_key="k1", workspace_id="w1"))
    view = server.router.get_task(tid)
    assert view.request.workspace_seq == 2  # stamped at head
    # force flush (same call the server result path makes)
    await server.live_workspaces.force_flush("w1")
    assert ws.dirty is False
    commits = server.live_workspaces.git_history("w1")
    assert any("live checkpoint" in c["message"] for c in commits)


def test_code_task_embeds_live_files(tmp_path):
    from common.types import TaskType
    server = make_server(tmp_path)
    server.live_workspaces.create("w1", {"m.py": "LIVE\n"})
    req = TaskRequest(prompt="fix", idempotency_key="k1", workspace_id="w1",
                      task_type=TaskType.CODE_EDIT,
                      code={"files": {"m.py": "STALE\n"},
                            "test_cmd": ["pytest", "-q"]})
    asyncio.run(server.submit_task(req))
    view = server.router.get_task(req.task_id)
    assert "LIVE" in view.request.prompt
    assert "STALE" not in view.request.prompt


def test_ws_patch_requires_auth(tmp_path):
    server = make_server(tmp_path)
    client = TestClient(server.app)
    server.tokens["tok"] = "n1"
    server.live_workspaces.create("w1", {"m.py": "x\n"})
    with client.websocket_connect("/ws/workspace/w1") as anon:
        anon.receive_text()
        anon.send_text(json.dumps({
            "type": "workspace.patch", "path": "m.py", "base_version": 1,
            "ops": [{"op": "insert", "line": 2, "text": "y\n"}]}))
        err = json.loads(anon.receive_text())
        assert err["type"] == "workspace.error"
    with client.websocket_connect("/ws/workspace/w1?token=tok") as authed:
        authed.receive_text()
        authed.send_text(json.dumps({
            "type": "workspace.patch", "path": "m.py", "base_version": 1,
            "ops": [{"op": "insert", "line": 2, "text": "y\n"}]}))
        d = json.loads(authed.receive_text())
        assert d["seq"] == 2


def test_rest_patch_endpoint(tmp_path):
    """One-shot REST write: sequenced, rebased, auth-gated."""
    server = make_server(tmp_path)
    client = TestClient(server.app)
    server.tokens["tok"] = "n1"
    server.live_workspaces.create("w1", {"a.py": "x\n"})
    hdr = {"Authorization": "Bearer tok"}

    # no auth -> 401
    r = client.post("/v1/workspaces/w1/patch", json={
        "path": "a.py", "base_version": 1,
        "ops": [{"op": "insert", "line": 2, "text": "y\n"}]})
    assert r.status_code == 401

    # authed write applies and bumps seq/version
    r = client.post("/v1/workspaces/w1/patch", headers=hdr, json={
        "path": "a.py", "base_version": 1,
        "ops": [{"op": "insert", "line": 2, "text": "y\n"}]})
    assert r.status_code == 200
    ev = r.json()
    assert ev["seq"] == 2 and ev["version"] == 2 and not ev["rebased"]
    st = client.get("/v1/workspaces/w1/file",
                    params={"path": "a.py"}).json()
    assert st["text"] == "x\ny\n"

    # stale base rebases, never rejects
    r = client.post("/v1/workspaces/w1/patch", headers=hdr, json={
        "path": "a.py", "base_version": 1,
        "ops": [{"op": "insert", "line": 2, "text": "z\n"}]})
    assert r.status_code == 200 and r.json()["rebased"]

    # unknown workspace -> 404, bad body -> 422
    assert client.post("/v1/workspaces/nope/patch", headers=hdr,
                       json={"path": "a.py", "ops": []}).status_code == 404
    assert client.post("/v1/workspaces/w1/patch", headers=hdr,
                       json={"ops": []}).status_code == 422


def test_rest_patch_broadcasts_to_ws_watchers(tmp_path):
    """A REST write lands as a live delta on open workspace sockets."""
    server = make_server(tmp_path)
    client = TestClient(server.app)
    server.tokens["tok"] = "n1"
    server.live_workspaces.create("w1", {"a.py": "x\n"})
    with client.websocket_connect("/ws/workspace/w1") as watcher:
        snap = json.loads(watcher.receive_text())
        assert snap["type"] == "workspace.snapshot"
        r = client.post("/v1/workspaces/w1/patch",
                        headers={"Authorization": "Bearer tok"},
                        json={"path": "a.py", "base_version": 1,
                              "ops": [{"op": "replace_file",
                                       "text": "shared\n"}]})
        assert r.status_code == 200
        delta = json.loads(watcher.receive_text())
        assert delta["type"] == "workspace.delta"
        assert delta["seq"] == 2 and delta["actor"] == "n1"


def test_rest_export_bundle(tmp_path):
    """Docs-sync surface: export returns files + commits, filterable."""
    server = make_server(tmp_path)
    client = TestClient(server.app)
    server.tokens["tok"] = "n1"
    server.live_workspaces.create("w1", {"a.md": "# hello\n", "b.md": "x\n"})
    hdr = {"Authorization": "Bearer tok"}
    client.post("/v1/workspaces/w1/patch", headers=hdr, json={
        "path": "a.md", "base_version": 1,
        "ops": [{"op": "insert", "line": 2, "text": "more\n"}]})
    r = client.get("/v1/workspaces/w1/export")
    assert r.status_code == 200
    body = r.json()
    assert body["files"]["a.md"] == "# hello\nmore\n"
    assert body["seq"] == 2 and isinstance(body["commits"], list)
    r = client.get("/v1/workspaces/w1/export", params={"paths": "b.md"})
    assert list(r.json()["files"]) == ["b.md"]
    assert client.get("/v1/workspaces/nope/export").status_code == 404
