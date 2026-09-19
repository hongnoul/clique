"""Python SDK for the server node API (MVP implementation)."""

from __future__ import annotations

import asyncio
import uuid

import httpx

from common.types import Cluster, NodeInfo, TaskRequest, TaskState, TaskView


class CliqueClient:
    def __init__(self, base_url: str, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s

    @classmethod
    async def discover(cls, timeout_s: float = 5.0) -> "CliqueClient":
        from node.discovery import find_server
        ann = await find_server(timeout_s=timeout_s)
        if ann is None:
            raise ConnectionError("no clique server found on this network")
        return cls(f"http://{ann.api_address}")

    async def _get(self, path: str, **params) -> dict | list:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(f"{self.base_url}{path}", params=params or None)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(f"{self.base_url}{path}", json=body)
            r.raise_for_status()
            return r.json()

    # -- tasks ---------------------------------------------------------------

    async def submit(self, prompt: str, *, task_type: str = "chat",
                     model_hint: str | None = None,
                     max_output_tokens: int = 1024) -> str:
        request = TaskRequest(
            prompt=prompt, model_hint=model_hint,
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
