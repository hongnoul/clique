"""WebSocket endpoints of the server node.

Channels:
- /ws/agent          node agents: assignment push, revoke, context sync
                     (server->node), task progress/results (node->server).
                     Outbound-dial from the agent so nodes need no
                     inbound ports.
- /ws/events         dashboard/CLI event firehose: node joins/leaves,
                     status changes, task state transitions, suggestion
                     updates, queue stats ticks.
- /ws/sessions/{id}  live token stream of one session (multi-viewer
                     future: any authorized watcher may attach).
"""

from __future__ import annotations


def register_ws_routes(app: "FastAPI", server: "SchedulerServer") -> None:
    """Attach the three WS endpoints. Each validates the session token
    on connect and closes with a protocol error code on auth failure."""
    ...


class AgentChannel:
    """Server-side handler for one connected node agent."""

    async def send_assignment(self, msg: "MsgAssignTask") -> bool:
        """Push an assignment; returns True on node ACK within the
        reservation window (router requirement)."""
        ...

    async def send_revoke(self, msg: "MsgRevokeTask") -> None: ...

    async def on_message(self, envelope: "Envelope") -> None:
        """Dispatch heartbeat/progress/result/leave to the right
        subsystem after signature verification."""
        ...


class EventBroadcaster:
    """Fan-out of server events to dashboard/CLI subscribers with
    per-subscriber backpressure (drop-oldest for stats ticks, never drop
    task state transitions)."""

    async def publish(self, event_type: str, payload: dict) -> None: ...
