"""P1 tests: end-to-end CODE_EDIT through the server harness gate.

Uses the live-server fixture style (real uvicorn + real agent over
HTTP/WS) but stubs the agent runtime to emit a canned diff, proving the
server extracts, applies, test-verifies, and commits before SUCCEEDED.
A second case emits garbage (no fence) and must end FAILED without a
commit, exercising the retry path.
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
from common.types import TaskState
from node.agent import NodeAgent
from scheduler.server import SchedulerServer


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_config(tmp_path, port: int, name: str) -> Config:
    cfg = Config()
    cfg.node.display_name = name
    cfg.node.data_dir = tmp_path / name
    cfg.node.model_runtime = "echo"
    cfg.node.model_family = "echo"
    cfg.node.model_parameter_b = 7.0
    cfg.node.heartbeat_interval_s = 0.2
    cfg.server.api_host = "127.0.0.1"
    cfg.server.api_port = port
    cfg.server.db_path = tmp_path / "server.db"
    cfg.server.lease_seconds = 30.0
    return cfg


GOOD_DIFF_OUTPUT = """fix applied
```diff
--- a/foo.py
+++ b/foo.py
@@ -1 +1 @@
-old
+new
```
"""

BAD_OUTPUT = "here is some prose with no diff fence at all"


class CannedRuntime:
    """Stub runtime emitting one canned output then succeeding."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._cancel = None

    async def health(self) -> bool:
        return True

    async def infer_stream(self, prompt: str, max_tokens: int):
        yield self.text

    async def cancel(self) -> None:
        pass


@pytest_asyncio.fixture
async def code_clique(tmp_path):
    port = free_port()
    server = SchedulerServer(make_config(tmp_path, port, "server-node"))
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
    yield base, server
    uv.should_exit = True
    await asyncio.gather(server_task, return_exceptions=True)


async def run_agent(base: str, tmp_path, canned: str):
    cfg = make_config(tmp_path, 0, f"agent-{uuid.uuid4().hex[:6]}")
    agent = NodeAgent(cfg, base)
    agent.runtime = CannedRuntime(canned)
    task = asyncio.create_task(agent.run())
    return agent, task


async def wait_done(base: str, task_id: str, timeout: float = 20.0) -> dict:
    async with httpx.AsyncClient() as c:
        for _ in range(int(timeout / 0.2)):
            data = (await c.get(f"{base}/v1/tasks/{task_id}")).json()
            if data["state"] in ("succeeded", "failed", "cancelled", "expired"):
                return data
            await asyncio.sleep(0.2)
    raise TimeoutError(task_id)


@pytest.mark.asyncio
async def test_code_patch_accepted(code_clique, tmp_path):
    base, server = code_clique
    agent, task = await run_agent(base, tmp_path, GOOD_DIFF_OUTPUT)
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(base + "/v1/code/tasks", json={
                "prompt": "change old to new",
                "code": {"files": {
                    "foo.py": "old\n",
                    "test_foo.py": ("def test_fix():\n"
                                    "    assert open('foo.py').read() == 'new\\n'\n")},
                    "test_cmd": ["pytest", "-q", "test_foo.py"]},
            })
            r.raise_for_status()
            task_id = r.json()["task_id"]
        data = await wait_done(base, task_id)
        # Workspace has foo.py (fixed) + test_foo.py asserting the fix,
        # so the harness gate must pass end to end.
        assert data["state"] == "succeeded", data
        assert data["result"]["patch"].startswith("--- a/foo.py")
        assert data["result"]["applied_sha"]
        assert data["result"]["test_report"]["returncode"] == 0
        async with httpx.AsyncClient() as c2:
            diff = (await c2.get(
                f"{base}/v1/code/tasks/{task_id}/diff")).json()
            assert "foo.py" in diff["patch"]
            assert diff["applied_sha"] == data["result"]["applied_sha"]
            tests = (await c2.get(
                f"{base}/v1/code/tasks/{task_id}/tests")).json()
            assert tests["test_report"]["returncode"] == 0
        # code-repo history records the accept commit
        history = server.code_vcs.history(limit=5)
        assert any(task_id in h["message"] for h in history)
    finally:
        agent._stop.set()
        task.cancel()


@pytest.mark.asyncio
async def test_code_patch_rejected_no_fence(code_clique, tmp_path):
    base, server = code_clique
    agent, task = await run_agent(base, tmp_path, BAD_OUTPUT)
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(base + "/v1/code/tasks", json={
                "prompt": "do something",
                "code": {"files": {"foo.py": "old\n"},
                         "test_cmd": ["pytest", "-q"]},
            })
            task_id = r.json()["task_id"]
        data = await wait_done(base, task_id)
        assert data["state"] == TaskState.FAILED.value
        assert "diff_missing" in (data["result"]["error"] or "")
    finally:
        agent._stop.set()
        task.cancel()


@pytest.mark.asyncio
async def test_ledger_records_accepted(code_clique, tmp_path):
    """Accepted code task credits $0.20 in ledger + /v1/stats."""
    base, server = code_clique
    agent, task = await run_agent(base, tmp_path, GOOD_DIFF_OUTPUT)
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(base + "/v1/code/tasks", json={
                "prompt": "change old to new",
                "code": {"files": {
                    "foo.py": "old\n",
                    "test_foo.py": ("def test_fix():\n"
                                    "    assert open('foo.py').read() == 'new\\n'\n")},
                    "test_cmd": ["pytest", "-q", "test_foo.py"]},
            })
            task_id = r.json()["task_id"]
        data = await wait_done(base, task_id)
        assert data["state"] == "succeeded", data
        async with httpx.AsyncClient() as c:
            ledger = (await c.get(base + "/v1/ledger")).json()
            assert ledger["accepted_tasks"] == 1
            assert ledger["earned_run_rate"] == 0.20
            stats = (await c.get(base + "/v1/stats")).json()
            assert stats["ledger"]["accepted_tasks"] == 1
    finally:
        agent._stop.set()
        task.cancel()


@pytest.mark.asyncio
async def test_code_race_first_wins(code_clique, tmp_path):
    """Two members, one good diff + one garbage: winner accepted, no crash."""
    base, server = code_clique
    good_agent, good_task = await run_agent(base, tmp_path, GOOD_DIFF_OUTPUT)
    bad_agent, bad_task = await run_agent(base, tmp_path, BAD_OUTPUT)
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(base + "/v1/code/race", json={
                "prompt": "change old to new",
                "code": {"files": {
                    "foo.py": "old\n",
                    "test_foo.py": ("def test_fix():\n"
                                    "    assert open('foo.py').read() == 'new\\n'\n")},
                    "test_cmd": ["pytest", "-q", "test_foo.py"]},
                "fanout": 2,
            })
            r.raise_for_status()
            body = r.json()
            assert len(body["task_ids"]) == 2
        results = [await wait_done(base, tid) for tid in body["task_ids"]]
        states = {r["state"] for r in results}
        assert "succeeded" in states  # winner accepted
    finally:
        for a, t in ((good_agent, good_task), (bad_agent, bad_task)):
            a._stop.set()
            t.cancel()


def test_gc_workspaces(tmp_path):
    """Orphaned workspace dirs older than threshold are removed."""
    import time
    from scheduler.server import SchedulerServer
    from common.config import Config
    cfg = Config()
    cfg.node.display_name = "gc-test"
    cfg.node.data_dir = tmp_path / "srv"
    cfg.server.db_path = tmp_path / "server.db"
    server = SchedulerServer(cfg)
    orphan = server.workspaces.base_dir / "t-orphan"
    orphan.mkdir(parents=True)
    (orphan / "x.py").write_text("x")
    old = time.time() - 7200
    import os
    os.utime(orphan, (old, old))
    assert server.gc_workspaces(older_than_s=3600) == 1
    assert not orphan.exists()


TOOL_ROUNDS = [
    '{"op": "read", "path": "foo.py"}\n',
    '{"op": "edit", "path": "foo.py", "old": "old", "new": "new"}\n',
    ('final\n```diff\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n'
     '-old\n+new\n```\n'),
]


@pytest.mark.asyncio
async def test_tool_loop_e2e_accepted(code_clique, tmp_path):
    """Scripted 3-round tool loop produces an accepted patch + ledger.

    Proves the production path: use_tools prompt triggers the executor
    loop on the node, final diff passes the server harness gate.
    """
    base, server = code_clique

    class LoopRuntime:
        def __init__(self):
            self.rounds = list(TOOL_ROUNDS)

        async def health(self):
            return True

        async def infer_stream(self, prompt, max_tokens):
            assert "TOOLS:" in prompt  # trigger reached the node
            yield self.rounds.pop(0) if self.rounds else '{"op":"done"}\n'

        async def cancel(self):
            pass

    from node.agent import NodeAgent
    from common.config import Config as _Config
    cfg = _Config()
    cfg.node.display_name = "tool-node"
    cfg.node.data_dir = tmp_path / "tool-node"
    cfg.node.model_runtime = "echo"
    cfg.node.model_family = "echo"
    cfg.node.model_parameter_b = 7.0
    cfg.node.heartbeat_interval_s = 0.2
    agent = NodeAgent(cfg, base)
    agent.runtime = LoopRuntime()
    task = asyncio.create_task(agent.run())
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(base + "/v1/code/tasks", json={
                "prompt": "change old to new",
                "code": {"files": {
                    "foo.py": "old\n",
                    "test_foo.py": ("def test_fix():\n"
                                    "    assert open('foo.py').read() == 'new\\n'\n")},
                    "test_cmd": ["pytest", "-q", "test_foo.py"],
                    "use_tools": True},
            })
            r.raise_for_status()
            task_id = r.json()["task_id"]
        data = await wait_done(base, task_id)
        assert data["state"] == "succeeded", data
        assert data["result"]["applied_sha"]
        ledger = (await httpx.AsyncClient().get(
            base + "/v1/ledger")).json()
        assert ledger["accepted_tasks"] >= 1
    finally:
        agent._stop.set()
        task.cancel()
