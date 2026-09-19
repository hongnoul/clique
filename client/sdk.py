"""Python SDK for the server node API. Used by the CLI, the TUI
dashboard, and any node-local tooling (jcode integration)."""

from __future__ import annotations

from typing import AsyncIterator

from common.types import (
    CronJob,
    NodeInfo,
    Session,
    TaskRequest,
    TaskResult,
)


class CliqueClient:
    """Async client over httpx/websockets."""

    def __init__(self, base_url: str, session_token: str) -> None: ...

    @classmethod
    async def discover(cls) -> "CliqueClient":
        """Find the clique via node/discovery.find_server and
        authenticate with the local agent's stored token."""
        ...

    # --- tasks ---
    async def submit(self, request: TaskRequest) -> str: ...
    async def task(self, task_id: str) -> TaskResult: ...
    async def cancel(self, task_id: str) -> bool: ...
    async def chat(self, prompt: str, session_id: str | None = None) -> AsyncIterator[str]:
        """Convenience: submit + stream progress deltas until done."""
        ...

    # --- clique views ---
    async def nodes(self) -> list[NodeInfo]: ...
    async def sessions(self) -> list[Session]: ...
    async def stats(self) -> dict: ...
    async def suggestions(self) -> list[dict]: ...

    # --- events ---
    async def events(self) -> AsyncIterator[dict]:
        """Subscribe to /ws/events (auto-reconnect with resume offset)."""
        ...

    async def watch_session(self, session_id: str) -> AsyncIterator[str]:
        """Live token stream of a session (/ws/sessions/{id})."""
        ...

    # --- op actions ---
    async def op(self, target_node: str) -> None: ...
    async def deop(self, target_node: str) -> None: ...
    async def approve_cron(self, cron_id: str) -> CronJob: ...
    async def request_cron(self, cron_expr: str, template: TaskRequest) -> CronJob: ...
