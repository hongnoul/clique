"""Server node (full implementation).

FastAPI app exposing:
- POST /v1/register           signed node registration -> token
- WS   /ws/agent?token=       agent channel (heartbeat/assign/result)
- POST /v1/tasks              submit task
- GET  /v1/tasks/{id}         task view (state + result)
- POST /v1/tasks/{id}/cancel
- GET  /v1/tasks              list
- GET  /v1/nodes, /v1/clusters, /v1/clique, /v1/stats
- extended surface (scheduler/api/rest.py): sessions, permissions,
  cron, suggestions, vcs, kick, /v1/chat/completions, /dash
- WS firehose (scheduler/api/ws.py): /ws/events, /ws/sessions/{id}

A background loop schedules queued tasks onto ready agent connections,
expires leases, marks stale nodes offline, fires approved cron jobs,
runs suggestion analysis, and takes periodic vcs snapshots.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from datetime import timedelta

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from common import protocol
from common.config import Config
from common.types import (
    ModelSpec,
    NodeRole,
    NodeStatus,
    ResourceSnapshot,
    TaskRequest,
    TaskResult,
    TaskState,
    node_id_from_public_key,
    utcnow,
)
from scheduler.api.rest import register_extended_routes
from scheduler.api.ws import EventBroadcaster, register_ws_routes
from scheduler.context_store import ContextStore
from scheduler.cron import CronService
from scheduler.permissions import PermissionManager
from scheduler.registry import Registry
from scheduler.router import Router
from scheduler.sessions import SessionManager
from scheduler.suggestions import SuggestionEngine
from scheduler.vcs import VcsService

log = logging.getLogger("clique.server")


class AgentConnections:
    """Live WS connections keyed by node_id."""

    def __init__(self) -> None:
        self._ws: dict[str, WebSocket] = {}

    def add(self, node_id: str, ws: WebSocket) -> None:
        self._ws[node_id] = ws

    def remove(self, node_id: str, ws: WebSocket) -> None:
        if self._ws.get(node_id) is ws:
            del self._ws[node_id]

    def get(self, node_id: str) -> WebSocket | None:
        return self._ws.get(node_id)

    def connected_ids(self) -> set[str]:
        return set(self._ws)


class SchedulerServer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.registry = Registry(config.server.db_path)
        self.router = Router(
            self.registry, config.server.db_path,
            lease_seconds=config.server.lease_seconds,
            max_attempts=config.server.max_attempts,
            queue_cap=config.server.queue_cap,
        )
        self.conns = AgentConnections()
        self.tokens: dict[str, str] = {}  # token -> node_id
        self.progress: dict[str, str] = {}  # task_id -> accumulated text
        self._tick_task: asyncio.Task | None = None

        # extended subsystems (P2/P3)
        db = config.server.db_path
        self.events = EventBroadcaster()
        self.permissions = PermissionManager(
            db, self.registry, policy=config.server.permission_policy)
        self.contexts = ContextStore(db)
        self.sessions = SessionManager(db, self.contexts, self.registry)
        self.cron = CronService(db, self.router, self.permissions)
        self.suggestions = SuggestionEngine(self.registry, self.router)
        self.vcs = VcsService(config.node.data_dir / "state-repo")
        self.vcs.register("permissions.json", self.permissions.export_state)
        self.vcs.register("cron.json", self.cron.export_state)
        self.vcs.register("sessions.json", self.sessions.export_state)
        self.vcs.register("nodes.json", self._export_nodes)
        self.vcs.init()
        self.permissions.on_change(
            lambda action, actor, target: self.vcs.snapshot(
                f"{action} {target or ''} by {actor[:8]}", actor))

        self.app = self._build_app()

    def _export_nodes(self) -> str:
        import json
        return json.dumps(
            [{"node_id": n.node_id, "display_name": n.display_name,
              "role": n.role.value, "op_level": n.op_level.value,
              "model": n.model.cluster_key() if n.model else None}
             for n in self.registry.list_nodes()], indent=2, sort_keys=True)

    async def submit_task(self, request: TaskRequest) -> str:
        """Submit + immediate scheduling; shared by REST routes."""
        if request.session_id:
            try:
                session = self.sessions.get(request.session_id)
            except KeyError:
                raise HTTPException(404, "no such session")
            version = self.contexts.latest_version(request.session_id)
            self.sessions.append_turn(
                request.session_id, version, "user", request.prompt)
            if not request.model_hint and session.cluster_key:
                request.model_hint = session.cluster_key
        try:
            task_id = self.router.submit(request)
        except OverflowError as e:
            raise HTTPException(429, str(e)) from e
        await self.events.publish("task.submitted", {"task_id": task_id})
        await self._schedule_now()
        return task_id

    # ------------------------------------------------------------------ app

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="clique-server")

        @app.on_event("startup")
        async def _startup() -> None:
            self._tick_task = asyncio.create_task(self._tick_loop())

        @app.on_event("shutdown")
        async def _shutdown() -> None:
            if self._tick_task:
                self._tick_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._tick_task

        @app.post("/v1/register")
        async def register(body: dict) -> dict:
            public_key = body["public_key"]
            signature = body["signature"]
            # prove keypair ownership: signature over the display_name bytes
            protocol.verify_payload(
                body["display_name"].encode(), signature, public_key)
            node_id = node_id_from_public_key(public_key)
            model = ModelSpec.model_validate(body["model"]) if body.get("model") else None
            resources = (ResourceSnapshot.model_validate(body["resources"])
                         if body.get("resources") else None)
            info = self.registry.register(
                node_id=node_id, display_name=body["display_name"],
                public_key=public_key, address=body.get("address", ""),
                model=model, resources=resources,
                role=NodeRole(body.get("role", "client")),
            )
            token = secrets.token_urlsafe(24)
            self.tokens[token] = node_id
            await self.events.publish("node.joined", {
                "node_id": node_id, "display_name": body["display_name"],
                "op_level": info.op_level.value})
            return {"node_id": node_id, "token": token,
                    "op_level": info.op_level.value,
                    "default_model": self.config.server.default_model}

        @app.websocket("/ws/agent")
        async def agent_ws(ws: WebSocket, token: str = Query(...)) -> None:
            node_id = self.tokens.get(token)
            if node_id is None:
                await ws.close(code=4401)
                return
            await ws.accept()
            self.conns.add(node_id, ws)
            try:
                while True:
                    msg = protocol.loads(await ws.receive_text())
                    await self._on_agent_message(node_id, msg)
            except WebSocketDisconnect:
                pass
            finally:
                self.conns.remove(node_id, ws)

        @app.post("/v1/tasks")
        async def submit(body: dict) -> dict:
            request = TaskRequest.model_validate(body)
            return {"task_id": await self.submit_task(request)}

        @app.get("/v1/tasks/{task_id}")
        async def get_task(task_id: str) -> dict:
            view = self.router.get_task(task_id)
            if view is None:
                raise HTTPException(404, "no such task")
            out = view.model_dump(mode="json")
            out["partial_output"] = self.progress.get(task_id, "")
            return out

        @app.post("/v1/tasks/{task_id}/cancel")
        async def cancel(task_id: str) -> dict:
            view = self.router.get_task(task_id)
            if view is None:
                raise HTTPException(404, "no such task")
            cancelled = self.router.cancel(task_id)
            if cancelled and view.assigned_node:
                ws = self.conns.get(view.assigned_node)
                if ws is not None:
                    await ws.send_text(protocol.dumps(protocol.msg_revoke(
                        task_id, view.attempt_id or "", "cancelled by user")))
                self.registry.set_status(
                    view.assigned_node, NodeStatus.READY, current_task_id=None)
            return {"cancelled": cancelled}

        @app.get("/v1/tasks")
        async def list_tasks(state: str | None = None) -> list[dict]:
            return [v.model_dump(mode="json") for v in self.router.list_tasks(state)]

        @app.get("/v1/nodes")
        async def nodes() -> list[dict]:
            return [n.model_dump(mode="json") for n in self.registry.list_nodes()]

        @app.get("/v1/clusters")
        async def clusters() -> list[dict]:
            return [c.model_dump(mode="json") for c in self.registry.list_clusters()]

        @app.get("/v1/clique")
        async def clique() -> dict:
            return {"name": self.config.server.clique_name,
                    "default_model": self.config.server.default_model,
                    "policy": self.permissions.policy}

        @app.get("/v1/stats")
        async def stats() -> dict:
            s = self.router.queue_stats()
            s["nodes"] = {n.node_id: n.status.value for n in self.registry.list_nodes()}
            return s

        # ------------------------------------------------ headless curl flow
        # Zero-install TUI for monitor-less nodes: pure curl, or
        # curl + system python3 (stdlib only). No pip, no clone.

        @app.get("/", response_class=PlainTextResponse)
        async def index_txt(request: Request) -> str:
            base = str(request.base_url).rstrip("/")
            return (
                "clique headless access (pick one):\n"
                f"  snapshot:  curl -s {base}/dash.txt\n"
                f"  live loop: watch -n 2 curl -s {base}/dash.txt\n"
                f"  live TUI:  curl -fsSL {base}/tui.py -o /tmp/clique-tui.py"
                " && python3 /tmp/clique-tui.py --server "
                f"{base}\n"
                f"  one-liner: curl -fsSL {base}/tui.py | python3 - --server "
                f"{base}\n"
                f"  snapshot once via python: curl -fsSL {base}/tui.py | python3 -"
                f" --server {base} --once\n"
                f"  full CLI install: curl -fsSL {base}/join.sh | sh\n"
                "  (repo is private: export CLIQUE_GITHUB_TOKEN=github_pat_...\n"
                "   with contents:read first, or the clone step aborts)\n"
            )

        @app.get("/dash.txt", response_class=PlainTextResponse)
        async def dash_txt() -> str:
            """Plain-text dashboard snapshot: curl-only, no python needed."""
            import io
            from client.curl_tui import render_lines
            snap = {
                "clique": {
                    "name": self.config.server.clique_name,
                    "policy": self.config.server.permission_policy,
                    "default_model": self.config.server.default_model,
                },
                "nodes": [n.model_dump(mode="json")
                          for n in self.registry.list_nodes()],
                "clusters": [c.model_dump(mode="json")
                             for c in self.registry.list_clusters()],
                "stats": self.router.queue_stats(),
                "tasks": [v.model_dump(mode="json")
                          for v in self.router.list_tasks()],
            }
            buf = io.StringIO()
            buf.write(f"clique @ {utcnow().isoformat(timespec='seconds')}\n")
            buf.write("\n".join(render_lines(snap, interactive=False)))
            buf.write("\n")
            return buf.getvalue()

        @app.get("/tui.py", response_class=PlainTextResponse)
        async def tui_py() -> str:
            """Stdlib-only live TUI source: curl | python3, no install."""
            from pathlib import Path
            return (Path(__file__).resolve().parents[1] / "client"
                    / "curl_tui.py").read_text()

        @app.get("/join", response_class=PlainTextResponse)
        async def join_page(request: Request) -> str:
            """Join page (spec: rest.py join assets): menu + one-liner."""
            # Alias of / with the spec'd path so /join works as documented.
            return await index_txt(request)

        @app.get("/join.sh", response_class=PlainTextResponse)
        async def join_sh(request: Request) -> str:
            """One-line full CLI installer pinned to this server.

            Serves scripts/bootstrap.sh verbatim with CLIQUE_SERVER pre-set,
            so the installer logic lives in exactly one place. Private-repo
            token support (CLIQUE_GITHUB_TOKEN) comes along automatically.
            """
            from pathlib import Path

            base = str(request.base_url).rstrip("/")
            script = (Path(__file__).resolve().parents[1] / "scripts"
                      / "bootstrap.sh").read_text()
            header = (
                "#!/bin/sh\n"
                "# full clique CLI install, server preconfigured to this node.\n"
                f"#   curl -fsSL {base}/join.sh | sh\n"
                "# private repo: export CLIQUE_GITHUB_TOKEN=github_pat_... first.\n"
                "# lightweight alternative (no install, live TUI only):\n"
                f"#   curl -fsSL {base}/tui.py | python3 - --server {base}\n"
                f"export CLIQUE_SERVER=\"{base}\"\n"
            )
            # Strip the bootstrap shebang (already emitted above) and its
            # trailing generic join hints; ours are server-pinned instead.
            lines = script.splitlines(keepends=True)
            if lines and lines[0].startswith("#!"):
                lines = lines[1:]
            cut = len(lines)
            for i, ln in enumerate(lines):
                if ln.startswith('echo "installed: $BIN_DIR/clique"'):
                    cut = i
                    break
            body = "".join(lines[:cut])
            footer = (
                "echo \"installed: $BIN_DIR/clique (server: $CLIQUE_SERVER)\"\n"
                "echo \"live TUI now:  clique dash --server $CLIQUE_SERVER\"\n"
                "echo \"join now:      clique join --server $CLIQUE_SERVER --runtime echo --param-b 7\"\n"
            )
            return header + body + footer

        register_extended_routes(app, self)
        register_ws_routes(app, self)
        return app

    # ------------------------------------------------------------- agent msgs

    async def _on_agent_message(self, node_id: str, msg: dict) -> None:
        mtype = msg["type"]
        if mtype == protocol.HEARTBEAT:
            self.registry.on_heartbeat(
                node_id,
                status=NodeStatus(msg["status"]),
                resources=(ResourceSnapshot.model_validate(msg["resources"])
                           if msg.get("resources") else None),
                model=ModelSpec.model_validate(msg["model"]) if msg.get("model") else None,
                current_task_id=msg.get("current_task_id"),
            )
            await self._schedule_now()
        elif mtype == protocol.PROGRESS:
            tid = msg["task_id"]
            self.progress[tid] = self.progress.get(tid, "") + msg["text_delta"]
            self.router.mark_running(tid, msg["attempt_id"])
            view = self.router.get_task(tid)
            if view and view.request.session_id:
                await self.events.publish(
                    "session.delta",
                    {"task_id": tid, "delta": msg["text_delta"]},
                    topic=f"session:{view.request.session_id}")
        elif mtype == protocol.RESULT:
            result = TaskResult.model_validate(msg["result"])
            try:
                committed = self.router.on_result(result)
            except Exception as e:  # stale attempt etc.
                log.warning("result rejected: %s", e)
                committed = False
            if committed:
                self.progress.pop(result.task_id, None)
                view = self.router.get_task(result.task_id)
                sid = view.request.session_id if view else None
                if sid and result.state == TaskState.SUCCEEDED and result.output:
                    version = self.contexts.latest_version(sid)
                    self.sessions.append_turn(
                        sid, version, "assistant", result.output)
                    self.sessions.pin(sid, node_id)
                    await self.events.publish(
                        "session.turn", {"session_id": sid,
                                         "task_id": result.task_id},
                        topic=f"session:{sid}")
                await self.events.publish("task.finished", {
                    "task_id": result.task_id, "state": result.state.value,
                    "node_id": node_id})
            self.registry.set_status(node_id, NodeStatus.READY, current_task_id=None)
            await self._schedule_now()
        elif mtype == protocol.LEAVE:
            self.registry.remove(node_id)
            for tid in self.router.on_node_lost(node_id):
                log.info("requeued %s after %s left", tid, node_id)
            await self.events.publish("node.left", {"node_id": node_id})
            await self._schedule_now()

    # ---------------------------------------------------------------- loops

    async def _schedule_now(self) -> None:
        for assignment, request in self.router.schedule_pending():
            ws = self.conns.get(assignment.node_id)
            if ws is None:
                # node registered but WS gone: requeue
                self.router.on_node_lost(assignment.node_id)
                continue
            self.registry.set_status(
                assignment.node_id, NodeStatus.BUSY,
                current_task_id=assignment.task_id)
            await self.events.publish("task.assigned", {
                "task_id": assignment.task_id, "node_id": assignment.node_id,
                "reason": assignment.reason})
            try:
                await ws.send_text(protocol.dumps(
                    protocol.msg_assign(assignment, request)))
            except Exception:
                self.router.on_node_lost(assignment.node_id)
                self.registry.set_status(
                    assignment.node_id, NodeStatus.OFFLINE, current_task_id=None)

    async def _tick_loop(self) -> None:
        hb = self.config.node.heartbeat_interval_s
        offline_after = timedelta(seconds=hb * self.config.server.heartbeat_offline_after)
        snapshot_every = 30.0  # seconds between periodic vcs snapshots
        last_snapshot = 0.0
        import time as _time
        while True:
            await asyncio.sleep(hb)
            try:
                for info in self.registry.mark_offline_stale(offline_after):
                    log.info("node %s offline (stale heartbeat)", info.node_id)
                    for tid in self.router.on_node_lost(info.node_id):
                        log.info("requeued %s", tid)
                    await self.events.publish(
                        "node.offline", {"node_id": info.node_id})
                self.router.expire_leases()
                for task_id in self.cron.due():
                    log.info("cron fired task %s", task_id)
                    await self.events.publish(
                        "cron.fired", {"task_id": task_id})
                for s in self.suggestions.analyze():
                    await self.events.publish("suggestion.new", s.to_dict())
                await self.events.publish(
                    "stats.tick", self.router.queue_stats())
                now = _time.monotonic()
                if now - last_snapshot > snapshot_every:
                    last_snapshot = now
                    self.vcs.snapshot("periodic state snapshot", "server")
                await self._schedule_now()
            except Exception:
                log.exception("tick failed")


def create_app(config: Config | None = None) -> FastAPI:
    from common.config import load
    return SchedulerServer(config or load()).app


def main() -> None:
    import argparse

    import uvicorn

    from common.config import load

    parser = argparse.ArgumentParser("clique-server")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--no-announce", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    config = load()
    if args.port:
        config.server.api_port = args.port
    if args.host:
        config.server.api_host = args.host

    server = SchedulerServer(config)

    async def run() -> None:
        handle = None
        if not args.no_announce:
            from common.config import generate_or_load_keypair
            from node.discovery import ServerAnnouncement, announce_server
            pub, _ = generate_or_load_keypair(config.node.data_dir)
            try:
                handle = await announce_server(ServerAnnouncement(
                    clique_name=config.server.clique_name,
                    api_address=f"{config.server.api_host}:{config.server.api_port}",
                    server_public_key_fingerprint=pub[:16],
                    protocol_version=protocol.PROTOCOL_VERSION,
                ))
                log.info("announced clique '%s' over mDNS", config.server.clique_name)
            except Exception as e:
                log.warning("mDNS announce failed (%s); continuing without", e)
        uv = uvicorn.Server(uvicorn.Config(
            server.app, host=config.server.api_host,
            port=config.server.api_port, log_level="info"))
        try:
            await uv.serve()
        finally:
            if handle:
                await handle.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
