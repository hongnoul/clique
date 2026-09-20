"""Workspace realtime routes: /ws/workspace/{id} + REST CRUD.

WS protocol (JSON, same shape as agent channel):
  client -> server:
    {"type": "workspace.patch", "path, "base_version", "ops", "actor"}
    {"type": "workspace.sync", "path?", "since_seq"}
    {"type": "workspace.presence", "actor", "path", "line"}  (droppable)
  server -> client:
    {"type": "workspace.delta", ...applied ops + version + seq}
    {"type": "workspace.state", ...full file text on sync/gap}
    {"type": "workspace.presence", ...}  (fanout)

Gap handling: server tags every delta with global seq. If a client
detects a skipped seq, it sends workspace.sync and gets full state.
Slow subscribers never lose file content: EventBroadcaster uses
drop-oldest only for presence, file deltas go direct over this socket.

Agent context: running agents get workspace.invalidate pushed over
their existing /ws/agent socket (see server._on_workspace_patch).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect

from common import protocol

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from scheduler.server import SchedulerServer


def _actor_ws(server: "SchedulerServer", ws: WebSocket) -> str:
    token = ws.query_params.get("token", "")
    node_id = server.tokens.get(token)
    return node_id or f"anon-{id(ws):x}"


def register_workspace_routes(app: "FastAPI", server: "SchedulerServer") -> None:
    @app.websocket("/ws/workspace/{workspace_id}")
    async def workspace_ws(ws: WebSocket, workspace_id: str) -> None:
        try:
            server.workspaces.get(workspace_id)
        except KeyError:
            await ws.close(code=4404)
            return
        await ws.accept()
        actor = _actor_ws(server, ws)
        topic = f"workspace:{workspace_id}"
        key, q = server.events.subscribe(topic)
        # send initial snapshot so the joiner has head versions
        snap = server.workspaces.snapshot(workspace_id)
        await ws.send_text(protocol.dumps({
            "type": "workspace.snapshot",
            "workspace_id": workspace_id,
            "seq": snap["seq"],
            "files": {p: {"version": f["version"]}
                      for p, f in snap["files"].items()},
        }))

        async def _pump() -> None:
            while True:
                msg = await q.get()
                await ws.send_text(json.dumps(msg["payload"]))

        import asyncio
        pump = asyncio.create_task(_pump())
        try:
            while True:
                raw = await ws.receive_text()
                msg = protocol.loads(raw)
                mtype = msg.get("type")
                if mtype == protocol.WS_PATCH:
                    event = await server.apply_workspace_patch(
                        workspace_id, msg["path"],
                        int(msg.get("base_version", 0)),
                        list(msg.get("ops", [])), actor)
                    # direct echo is covered by broadcast; no extra send
                elif mtype == protocol.WS_SYNC:
                    path = msg.get("path")
                    if path:
                        st = server.workspaces.file_state(workspace_id, path)
                        await ws.send_text(protocol.dumps(
                            protocol.msg_ws_state(
                                workspace_id, path, st["version"],
                                st["seq"], st["text"])))
                    else:
                        full = server.workspaces.snapshot(workspace_id)
                        await ws.send_text(protocol.dumps({
                            "type": "workspace.snapshot",
                            "workspace_id": workspace_id,
                            "seq": full["seq"],
                            "files": full["files"],
                        }))
                elif mtype == protocol.WS_PRESENCE:
                    await server.events.publish(
                        "workspace.presence",
                        protocol.msg_ws_presence(
                            workspace_id, actor,
                            msg.get("path"), msg.get("line")),
                        topic=topic)
                # unknown types ignored (forward-compat)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            pump.cancel()
            server.events.unsubscribe(key)

    # -- REST ----------------------------------------------------------

    @app.post("/v1/workspaces")
    async def create_workspace(body: dict, request: Request) -> dict:
        from scheduler.api.rest import _actor
        actor = _actor(server, request)
        ws = server.workspaces.create(
            workspace_id=body.get("workspace_id"),
            initial_files=body.get("files"))
        await server.events.publish("workspace.created", {
            "workspace_id": ws.workspace_id, "actor": actor})
        return {"workspace_id": ws.workspace_id, "seq": ws.seq}

    @app.get("/v1/workspaces")
    async def list_workspaces() -> list[dict]:
        return [{"workspace_id": wid,
                 "seq": server.workspaces.get(wid).seq}
                for wid in server.workspaces.list_ids()]

    @app.get("/v1/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str) -> dict:
        try:
            return server.workspaces.snapshot(workspace_id)
        except KeyError:
            raise HTTPException(404, "no such workspace")

    @app.get("/v1/workspaces/{workspace_id}/file")
    async def get_workspace_file(workspace_id: str, path: str) -> dict:
        try:
            return server.workspaces.file_state(workspace_id, path)
        except KeyError:
            raise HTTPException(404, "no such workspace")

    @app.post("/v1/workspaces/{workspace_id}/flush")
    async def flush_workspace(workspace_id: str, request: Request) -> dict:
        from scheduler.api.rest import _actor
        _actor(server, request)
        try:
            sha = await server.workspaces.force_flush(workspace_id)
        except KeyError:
            raise HTTPException(404, "no such workspace")
        return {"sha": sha}
