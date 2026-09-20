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
