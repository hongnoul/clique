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
    from common.types import TaskRequest
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
