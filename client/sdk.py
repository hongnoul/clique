"""Python SDK for the server node API (full implementation)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx

from common.types import Cluster, NodeInfo, TaskRequest, TaskState, TaskView


class CliqueClient:
    def __init__(self, base_url: str, timeout_s: float = 30.0,
                 token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self.token = token
        self.node_id: str | None = None

    @classmethod
    async def discover(cls, timeout_s: float = 5.0) -> "CliqueClient":
        from node.discovery import find_server
        ann = await find_server(timeout_s=timeout_s)
        if ann is None:
            raise ConnectionError("no clique server found on this network")
        return cls(f"http://{ann.api_address}")

    async def _get(self, path: str, **params) -> dict | list:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(f"{self.base_url}{path}", params=params or None,
                            headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(f"{self.base_url}{path}", json=body,
                             headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def _delete(self, path: str) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.delete(f"{self.base_url}{path}",
                               headers=self._headers())
            r.raise_for_status()
            return r.json()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def authenticate(self, data_dir: Path | None = None) -> str:
        """Register this device's keypair with the server to obtain a node
        token (required for op-gated routes). Reuses the agent identity
        when one exists in data_dir."""
        from common import protocol
        from common.config import DEFAULT_DIR, generate_or_load_keypair
        import socket
        pub, seed = generate_or_load_keypair(data_dir or DEFAULT_DIR)
        name = socket.gethostname().split(".")[0]
        body = {"display_name": name, "public_key": pub,
                "signature": protocol.sign_payload(name.encode(), seed),
                "role": "client"}
        data = await self._post("/v1/register", body)
        self.token = data["token"]
        self.node_id = data["node_id"]
        return self.token

    # -- tasks ---------------------------------------------------------------

    async def submit(self, prompt: str, *, task_type: str = "chat",
                     model_hint: str | None = None,
                     session_id: str | None = None,
                     max_output_tokens: int = 1024) -> str:
        request = TaskRequest(
            prompt=prompt, model_hint=model_hint, session_id=session_id,
            max_output_tokens=max_output_tokens,
            idempotency_key=uuid.uuid4().hex,
        )
        body = request.model_dump(mode="json")
        body["task_type"] = task_type
        return (await self._post("/v1/tasks", body))["task_id"]

    async def task(self, task_id: str) -> dict:
        return await self._get(f"/v1/tasks/{task_id}")  # includes partial_output

    async def wait(self, task_id: str, poll_s: float = 0.3,
                   timeout_s: float = 600.0) -> TaskView:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            data = await self.task(task_id)
            view = TaskView.model_validate(data)
            if view.state in (TaskState.SUCCEEDED, TaskState.FAILED,
                              TaskState.CANCELLED, TaskState.EXPIRED):
                return view
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"task {task_id} still {view.state.value}")
            await asyncio.sleep(poll_s)

    async def cancel(self, task_id: str) -> bool:
        return (await self._post(f"/v1/tasks/{task_id}/cancel"))["cancelled"]

    # -- clique views ----------------------------------------------------------

    async def nodes(self) -> list[NodeInfo]:
        return [NodeInfo.model_validate(n) for n in await self._get("/v1/nodes")]

    async def clusters(self) -> list[Cluster]:
        return [Cluster.model_validate(c) for c in await self._get("/v1/clusters")]

    async def clique(self) -> dict:
        return await self._get("/v1/clique")  # type: ignore[return-value]

    async def stats(self) -> dict:
        return await self._get("/v1/stats")  # type: ignore[return-value]

    # -- sessions --------------------------------------------------------------

    async def create_session(self, cluster_key: str = "") -> dict:
        return await self._post("/v1/sessions", {"cluster_key": cluster_key})

    async def sessions(self) -> list[dict]:
        return await self._get("/v1/sessions")  # type: ignore[return-value]

    async def session(self, session_id: str) -> dict:
        return await self._get(f"/v1/sessions/{session_id}")

    async def migrate_session(self, session_id: str, to_node: str,
                              reason: str = "") -> dict:
        return await self._post(f"/v1/sessions/{session_id}/migrate",
                                {"to_node": to_node, "reason": reason})

    async def close_session(self, session_id: str) -> dict:
        return await self._delete(f"/v1/sessions/{session_id}")

    # -- permissions -----------------------------------------------------------

    async def permissions(self) -> list[dict]:
        return await self._get("/v1/permissions")  # type: ignore[return-value]

    async def op(self, target: str) -> dict:
        return await self._post("/v1/permissions/op", {"target": target})

    async def deop(self, target: str) -> dict:
        return await self._post("/v1/permissions/deop", {"target": target})

    async def set_policy(self, policy: str) -> dict:
        return await self._post("/v1/permissions/policy", {"policy": policy})

    async def audit(self, limit: int = 100) -> list[dict]:
        return await self._get("/v1/permissions/audit", limit=limit)  # type: ignore

    async def kick(self, node_id: str) -> dict:
        return await self._post(f"/v1/nodes/{node_id}/kick")

    async def server_shutdown(self, confirm: bool = False) -> dict:
        """Raises httpx.HTTPStatusError(409) with response.json()["detail"]
        = {"active": [...]} if nodes have active tasks and confirm=False."""
        return await self._post("/v1/server/shutdown", {"confirm": confirm})

    # -- cron ------------------------------------------------------------------

    async def cron_request(self, cron_expr: str, prompt: str, **kw) -> dict:
        template = TaskRequest(prompt=prompt, idempotency_key="template", **kw)
        return await self._post("/v1/cron", {
            "cron_expr": cron_expr,
            "task_template": template.model_dump(mode="json")})

    async def cron_list(self) -> list[dict]:
        return await self._get("/v1/cron")  # type: ignore[return-value]

    async def cron_approve(self, cron_id: str) -> dict:
        return await self._post(f"/v1/cron/{cron_id}/approve")

    async def cron_reject(self, cron_id: str, reason: str = "") -> dict:
        return await self._post(f"/v1/cron/{cron_id}/reject", {"reason": reason})

    async def cron_disable(self, cron_id: str) -> dict:
        return await self._post(f"/v1/cron/{cron_id}/disable")

    # -- suggestions & vcs -----------------------------------------------------

    async def suggestions(self) -> list[dict]:
        return await self._get("/v1/suggestions")  # type: ignore[return-value]

    async def dismiss_suggestion(self, suggestion_id: str) -> dict:
        return await self._post(f"/v1/suggestions/{suggestion_id}/dismiss")

    async def vcs_history(self, limit: int = 50) -> list[dict]:
        return await self._get("/v1/vcs/history", limit=limit)  # type: ignore

    async def vcs_diff(self, a: str, b: str) -> str:
        return (await self._get("/v1/vcs/diff", a=a, b=b))["diff"]  # type: ignore

    async def vcs_rollback(self, sha: str) -> dict:
        return await self._post("/v1/vcs/rollback", {"sha": sha})
