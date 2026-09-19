"""WebSocket endpoints of the server node — implemented.

Channels:
- /ws/agent          (in scheduler/server.py) node agents: assignment
                     push, revoke, progress/results. Outbound-dial from
                     the agent so nodes need no inbound ports.
- /ws/events         dashboard/CLI event firehose: node joins/leaves,
                     status changes, task state transitions, suggestion
                     updates, queue stats ticks.
- /ws/sessions/{id}  live token stream of one session (multi-viewer:
                     any connected watcher receives every delta).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from scheduler.server import SchedulerServer


class EventBroadcaster:
    """Fan-out of server events to dashboard/CLI subscribers with
    per-subscriber backpressure: stats ticks are drop-oldest, task state
    transitions and governance events are never dropped."""

    DROPPABLE = {"stats.tick"}
    QUEUE_CAP = 256

    def __init__(self) -> None:
        self._subscribers: dict[object, asyncio.Queue] = {}
        # topic -> queues; "" = firehose, "session:<id>" = one session
        self._topics: dict[object, str] = {}

    def subscribe(self, topic: str = "") -> tuple[object, asyncio.Queue]:
        key = object()
        q: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_CAP)
        self._subscribers[key] = q
        self._topics[key] = topic
        return key, q

    def unsubscribe(self, key: object) -> None:
        self._subscribers.pop(key, None)
        self._topics.pop(key, None)

    async def publish(self, event_type: str, payload: dict,
                      topic: str = "") -> None:
        msg = {"event": event_type, "payload": payload}
        for key, q in list(self._subscribers.items()):
            if self._topics.get(key) != topic:
                continue
            if q.full():
                if event_type in self.DROPPABLE:
                    continue  # drop this tick for the slow subscriber
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # drop-oldest to make room
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(msg)


def register_ws_routes(app: "FastAPI", server: "SchedulerServer") -> None:
    """Attach /ws/events and /ws/sessions/{id}."""

    @app.websocket("/ws/events")
    async def events_ws(ws: WebSocket) -> None:
        await ws.accept()
        key, q = server.events.subscribe("")
        try:
            while True:
                msg = await q.get()
                await ws.send_text(json.dumps(msg))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            server.events.unsubscribe(key)

    @app.websocket("/ws/sessions/{session_id}")
    async def session_ws(ws: WebSocket, session_id: str) -> None:
        try:
            server.sessions.get(session_id)
        except KeyError:
            await ws.close(code=4404)
            return
        await ws.accept()
        key, q = server.events.subscribe(f"session:{session_id}")
        try:
            while True:
                msg = await q.get()
                await ws.send_text(json.dumps(msg))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            server.events.unsubscribe(key)
