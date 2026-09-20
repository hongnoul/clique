"""Workspace realtime routes: /ws/workspace/{id} + REST CRUD.

Auth split (deliberate, LAN demo):
- Reads are open: GET /v1/workspaces*, WS snapshot/sync/delta/presence.
  Any viewer on the LAN can watch collab without a token.
- Writes are gated: POST /v1/workspaces, POST .../flush require
  ``Authorization: Bearer`` (see rest._actor, 401 otherwise).
- WS patches carry an optional ``?token=``: resolved to node_id when
  present, else attributed as ``anon-<id>`` for the demo screen.
  Do not add a WS handshake here: the realtime loop is owned by the
  collab optimizer agent; standardize on ``?token=`` optional.

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
            server.live_workspaces.get(workspace_id)
        except KeyError:
            await ws.close(code=4404)
            return
        await ws.accept()
        actor = _actor_ws(server, ws)
        authed = actor in server.tokens.values()
        topic = f"workspace:{workspace_id}"
        key, q = server.events.subscribe(topic)
        # send initial snapshot so the joiner has head versions
        snap = server.live_workspaces.snapshot(workspace_id)
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
                    if not authed:
                        await ws.send_text(protocol.dumps({
                            "type": "workspace.error",
                            "workspace_id": workspace_id,
                            "error": "auth required: connect with ?token="}))
                        continue
                    try:
                        event = await server.apply_workspace_patch(
                            workspace_id, msg["path"],
                            int(msg.get("base_version", 0)),
                            list(msg.get("ops", [])), actor)
                    except ValueError as e:
                        await ws.send_text(protocol.dumps({
                            "type": "workspace.error",
                            "workspace_id": workspace_id,
                            "error": str(e)}))
                    # direct echo is covered by broadcast; no extra send
                elif mtype == protocol.WS_SYNC:
                    path = msg.get("path")
                    if path:
                        st = server.live_workspaces.file_state(workspace_id, path)
                        await ws.send_text(protocol.dumps(
                            protocol.msg_ws_state(
                                workspace_id, path, st["version"],
                                st["seq"], st["text"])))
                    else:
                        full = server.live_workspaces.snapshot(workspace_id)
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
        try:
            ws = server.live_workspaces.create(
                workspace_id=body.get("workspace_id"),
                initial_files=body.get("files"))
        except ValueError as e:
            raise HTTPException(422, str(e))
        await server.events.publish("workspace.created", {
            "workspace_id": ws.workspace_id, "actor": actor})
        return {"workspace_id": ws.workspace_id, "seq": ws.seq}

    @app.get("/v1/workspaces")
    async def list_workspaces() -> list[dict]:
        return [{"workspace_id": wid,
                 "seq": server.live_workspaces.get(wid).seq}
                for wid in server.live_workspaces.list_ids()]

    @app.get("/v1/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str) -> dict:
        try:
            return server.live_workspaces.snapshot(workspace_id)
        except KeyError:
            raise HTTPException(404, "no such workspace")

    @app.get("/v1/workspaces/{workspace_id}/file")
    async def get_workspace_file(workspace_id: str, path: str) -> dict:
        try:
            return server.live_workspaces.file_state(workspace_id, path)
        except KeyError:
            raise HTTPException(404, "no such workspace")

    @app.post("/v1/workspaces/{workspace_id}/patch")
    async def patch_workspace(workspace_id: str, body: dict,
                              request: Request) -> dict:
        """One-shot sequenced write (same semantics as WS workspace.patch).

        For CLI/MCP callers that don't hold a socket open. Body:
        ``{"path", "base_version", "ops"}`` where ops follow the
        insert/delete/replace_file grammar. Live WS subscribers still get
        the delta broadcast, and running agents get workspace.invalidate.
        """
        from scheduler.api.rest import _actor
        actor = _actor(server, request)
        try:
            server.live_workspaces.get(workspace_id)
        except KeyError:
            raise HTTPException(404, "no such workspace")
        try:
            event = await server.apply_workspace_patch(
                workspace_id, body["path"],
                int(body.get("base_version", 0)),
                list(body.get("ops", [])), actor)
        except (KeyError, TypeError):
            raise HTTPException(422, "body needs path + ops[]")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return event

    @app.post("/v1/workspaces/{workspace_id}/flush")
    async def flush_workspace(workspace_id: str, request: Request) -> dict:
        from scheduler.api.rest import _actor
        _actor(server, request)
        try:
            sha = await server.live_workspaces.force_flush(workspace_id)
        except KeyError:
            raise HTTPException(404, "no such workspace")
        return {"sha": sha}

    @app.get("/v1/workspaces/{workspace_id}/export")
    async def export_workspace(workspace_id: str,
                               paths: str | None = None) -> dict:
        """Docs-sync surface: full file contents + provenance bundle.

        Dogfood agents draft docs in the live workspace, then export
        the bundle and open a PR against the GitHub repo from a machine
        with credentials. Optional ?paths=a.md,b.md filters files.
        Reads are open (same as snapshot); no auth required.
        """
        try:
            snap = server.live_workspaces.snapshot(workspace_id)
        except KeyError:
            raise HTTPException(404, "no such workspace")
        wanted = None
        if paths:
            wanted = {p.strip() for p in paths.split(",") if p.strip()}
        files = {p: f["text"] for p, f in snap["files"].items()
                 if wanted is None or p in wanted}
        try:
            commits = server.live_workspaces.git_history(workspace_id, 10)
        except Exception:
            commits = []
        return {"workspace_id": workspace_id, "seq": snap["seq"],
                "files": files, "commits": commits}

    @app.get("/v1/workspaces/{workspace_id}/history")
    async def workspace_history(workspace_id: str, path: str | None = None,
                                limit: int = 50) -> list[dict]:
        try:
            return server.live_workspaces.history(workspace_id, path, limit)
        except KeyError:
            raise HTTPException(404, "no such workspace")

    @app.get("/v1/workspaces/{workspace_id}/commits")
    async def workspace_commits(workspace_id: str,
                                limit: int = 20) -> list[dict]:
        try:
            return server.live_workspaces.git_history(workspace_id, limit)
        except KeyError:
            raise HTTPException(404, "no such workspace")
