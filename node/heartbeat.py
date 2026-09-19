"""Heartbeat loop: periodic status/offer reporting to the server node."""

from __future__ import annotations

from common.protocol import MsgHeartbeat


class HeartbeatLoop:
    def __init__(self, server_url: str, session_token: str, interval_s: float) -> None: ...

    async def run(self) -> None:
        """Send MsgHeartbeat every interval with a fresh ResourceSnapshot
        (node/resources.probe) plus current status and task id.

        Backoff-and-retry on transient failures; after N consecutive
        failures invoke the on_server_lost callback so the agent
        rediscovers (node/discovery.watch_server)."""
        ...

    def build_heartbeat(self) -> MsgHeartbeat:
        """Assemble the message. Battery/pressure states here are what
        let the router lower this node's effective offer."""
        ...

    async def stop(self) -> None: ...
