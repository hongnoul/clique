"""REST API of the server node — implemented route registrars.

Core routes (register, tasks, nodes, clusters, clique, stats, join
assets) live in ``scheduler/server.py``. This module adds the extended
surface: sessions, permissions, cron, suggestions, vcs, kick, and the
OpenAI-compatible ``/v1/chat/completions`` adapter.

Auth: op-gated routes require ``Authorization: Bearer <token>`` from a
registered node; PermissionManager.check gates by OpLevel.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from common.errors import (
    NodeUnavailableError,
    PermissionError_,
    SessionConflictError,
)
from common.types import TaskRequest, TaskState

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from scheduler.server import SchedulerServer


def _actor(server: "SchedulerServer", request: Request) -> str:
    """Resolve the acting node from the bearer token. 401 when absent."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        node_id = server.tokens.get(auth[7:].strip())
        if node_id:
            return node_id
    raise HTTPException(401, "node token required (Authorization: Bearer)")


def register_extended_routes(app: "FastAPI", server: "SchedulerServer") -> None:
    """Attach sessions/permissions/cron/suggestions/vcs/chat routes."""

    # ---------------------------------------------------------------- sessions

    @app.post("/v1/sessions")
    async def create_session(body: dict, request: Request) -> dict:
        actor = _actor(server, request)
        session = server.sessions.create(
            owner_node=actor, cluster_key=body.get("cluster_key", ""))
        return session.model_dump(mode="json")

    @app.get("/v1/sessions")
    async def list_sessions() -> list[dict]:
        return [s.model_dump(mode="json") for s in server.sessions.list_active()]

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        try:
            session = server.sessions.get(session_id)
        except KeyError:
            raise HTTPException(404, "no such session")
        out = session.model_dump(mode="json")
        out["context"] = server.contexts.get_context(session_id).decode()
        return out

    @app.post("/v1/sessions/{session_id}/migrate")
    async def migrate_session(session_id: str, body: dict,
                              request: Request) -> dict:
        actor = _actor(server, request)
        try:
            session = server.sessions.get(session_id)
        except KeyError:
            raise HTTPException(404, "no such session")
        if actor != session.owner_node:
            try:
                server.permissions.check(actor, "migrate_session")
            except PermissionError_ as e:
                raise HTTPException(403, str(e))
        try:
            session = server.sessions.migrate(
                session_id, body["to_node"], body.get("reason", ""))
        except (NodeUnavailableError, SessionConflictError) as e:
            raise HTTPException(409, str(e))
        server.permissions.record(actor, "migrate_session", session_id,
                                  detail=body["to_node"])
        await server.events.publish("session.migrated",
                                    session.model_dump(mode="json"))
        return session.model_dump(mode="json")

    @app.delete("/v1/sessions/{session_id}")
    async def close_session(session_id: str, request: Request) -> dict:
        _actor(server, request)
        try:
            server.sessions.close(session_id)
        except KeyError:
            raise HTTPException(404, "no such session")
        return {"closed": True}

    # ------------------------------------------------------------- permissions

    @app.get("/v1/permissions")
    async def permissions() -> list[dict]:
        return server.permissions.levels()

    @app.post("/v1/permissions/op")
    async def grant_op(body: dict, request: Request) -> dict:
        actor = _actor(server, request)
        try:
            server.permissions.grant_op(actor, body["target"])
        except PermissionError_ as e:
            raise HTTPException(403, str(e))
        await server.events.publish("permission.granted",
                                    {"actor": actor, "target": body["target"]})
        return {"ok": True}

    @app.post("/v1/permissions/deop")
    async def revoke_op(body: dict, request: Request) -> dict:
        actor = _actor(server, request)
        try:
            server.permissions.revoke_op(actor, body["target"])
        except PermissionError_ as e:
            raise HTTPException(403, str(e))
        await server.events.publish("permission.revoked",
                                    {"actor": actor, "target": body["target"]})
        return {"ok": True}

    @app.post("/v1/permissions/policy")
    async def set_policy(body: dict, request: Request) -> dict:
        actor = _actor(server, request)
        try:
            server.permissions.set_policy(actor, body["policy"])
        except PermissionError_ as e:
            raise HTTPException(403, str(e))
        return {"policy": server.permissions.policy}

    @app.get("/v1/permissions/audit")
    async def audit(limit: int = 100) -> list[dict]:
        return server.permissions.audit_log(limit)

    # -------------------------------------------------------------------- kick

    @app.post("/v1/nodes/{node_id}/kick")
    async def kick(node_id: str, request: Request) -> dict:
        actor = _actor(server, request)
        try:
            server.permissions.check(actor, "kick_node")
        except PermissionError_ as e:
            raise HTTPException(403, str(e))
        if server.registry.get(node_id) is None:
            raise HTTPException(404, "no such node")
        ws = server.conns.get(node_id)
        if ws is not None:
            try:
                await ws.close(code=4403)
            except Exception:
                pass
        server.registry.remove(node_id)
        for tid in server.router.on_node_lost(node_id):
            pass  # requeued
        server.tokens = {t: n for t, n in server.tokens.items() if n != node_id}
        server.permissions.record(actor, "kick_node", node_id)
        await server.events.publish("node.kicked", {"node_id": node_id})
        return {"kicked": node_id}

    # -------------------------------------------------------------------- cron

    @app.post("/v1/cron")
    async def cron_request(body: dict, request: Request) -> dict:
        actor = _actor(server, request)
        template = TaskRequest.model_validate(body["task_template"])
        try:
            job = server.cron.request(actor, body["cron_expr"], template)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return job.model_dump(mode="json")

    @app.get("/v1/cron")
    async def cron_list(include_pending: bool = True) -> list[dict]:
        return [j.model_dump(mode="json")
                for j in server.cron.list_jobs(include_pending)]

    @app.post("/v1/cron/{cron_id}/approve")
    async def cron_approve(cron_id: str, request: Request) -> dict:
        actor = _actor(server, request)
        try:
            job = server.cron.approve(cron_id, actor)
        except PermissionError_ as e:
            raise HTTPException(403, str(e))
        except KeyError:
            raise HTTPException(404, "no such cron job")
        server.permissions.record(actor, "approve_cron", cron_id)
        return job.model_dump(mode="json")

    @app.post("/v1/cron/{cron_id}/reject")
    async def cron_reject(cron_id: str, body: dict, request: Request) -> dict:
        actor = _actor(server, request)
        try:
            server.cron.reject(cron_id, actor, body.get("reason", ""))
        except PermissionError_ as e:
            raise HTTPException(403, str(e))
        except KeyError:
            raise HTTPException(404, "no such cron job")
        return {"rejected": cron_id}

    @app.post("/v1/cron/{cron_id}/disable")
    async def cron_disable(cron_id: str, request: Request) -> dict:
        actor = _actor(server, request)
        try:
            server.cron.disable(cron_id, actor)
        except PermissionError_ as e:
            raise HTTPException(403, str(e))
        except KeyError:
            raise HTTPException(404, "no such cron job")
        return {"disabled": cron_id}

    # ------------------------------------------------------------- suggestions

    @app.get("/v1/suggestions")
    async def suggestions() -> list[dict]:
        return [s.to_dict() for s in server.suggestions.active()]

    @app.post("/v1/suggestions/{suggestion_id}/dismiss")
    async def dismiss_suggestion(suggestion_id: str, request: Request) -> dict:
        actor = _actor(server, request)
        try:
            server.suggestions.dismiss(suggestion_id, actor)
        except KeyError:
            raise HTTPException(404, "no such suggestion")
        return {"dismissed": suggestion_id}

    @app.get("/v1/suggestions/report")
    async def overload_report() -> dict:
        return server.suggestions.overload_report()

    # --------------------------------------------------------------------- vcs

    @app.get("/v1/vcs/history")
    async def vcs_history(limit: int = 50) -> list[dict]:
        return server.vcs.history(limit=limit)

    @app.get("/v1/vcs/diff")
    async def vcs_diff(a: str, b: str) -> dict:
        try:
            return {"diff": server.vcs.diff(a, b)}
        except KeyError:
            raise HTTPException(404, "unknown commit sha")

    @app.post("/v1/vcs/rollback")
    async def vcs_rollback(body: dict, request: Request) -> dict:
        actor = _actor(server, request)
        try:
            server.permissions.check(actor, "manage_vcs")
        except PermissionError_ as e:
            raise HTTPException(403, str(e))
        try:
            sha = server.vcs.rollback(body["sha"], actor)
        except KeyError:
            raise HTTPException(404, "unknown commit sha")
        server.permissions.record(actor, "vcs_rollback", body["sha"])
        return {"sha": sha}

    @app.post("/v1/vcs/snapshot")
    async def vcs_snapshot(body: dict, request: Request) -> dict:
        actor = _actor(server, request)
        sha = server.vcs.snapshot(body.get("message", "manual snapshot"), actor)
        return {"sha": sha}

    # ----------------------------------------------- OpenAI-compat completions

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(body: dict) -> dict | StreamingResponse:
        """Adapter: wraps a TaskRequest (+ optional session) so existing
        OpenAI-client tools can point at the clique as a provider."""
        messages = body.get("messages", [])
        if not messages:
            raise HTTPException(422, "messages required")
        prompt = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)
        session_id = body.get("session_id") or body.get("user")
        request = TaskRequest(
            prompt=prompt,
            session_id=session_id if session_id and
            session_id.startswith("s-") else None,
            model_hint=body.get("model") if body.get("model") not in
            (None, "", "clique", "auto") else None,
            max_output_tokens=body.get("max_tokens", 1024) or 1024,
            idempotency_key=uuid.uuid4().hex,
        )
        task_id = await server.submit_task(request)
        completion_id = f"chatcmpl-{task_id}"
        created = int(time.time())
        model_name = body.get("model") or "clique"

        async def wait_done(timeout_s: float = 600.0) -> dict:
            deadline = asyncio.get_event_loop().time() + timeout_s
            while True:
                view = server.router.get_task(task_id)
                if view and view.state in (
                        TaskState.SUCCEEDED, TaskState.FAILED,
                        TaskState.CANCELLED, TaskState.EXPIRED):
                    return view
                if asyncio.get_event_loop().time() > deadline:
                    raise HTTPException(504, "task timed out")
                await asyncio.sleep(0.1)

        if body.get("stream"):
            async def sse():
                import json as _json
                sent = 0
                while True:
                    view = server.router.get_task(task_id)
                    partial = server.progress.get(task_id, "")
                    done = view and view.state in (
                        TaskState.SUCCEEDED, TaskState.FAILED,
                        TaskState.CANCELLED, TaskState.EXPIRED)
                    if done and view.state == TaskState.SUCCEEDED and \
                            view.result and view.result.output:
                        partial = view.result.output
                    if len(partial) > sent:
                        chunk = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta":
                                         {"content": partial[sent:]},
                                         "finish_reason": None}]}
                        yield f"data: {_json.dumps(chunk)}\n\n"
                        sent = len(partial)
                    if done:
                        final = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta": {},
                                         "finish_reason": "stop"}]}
                        yield f"data: {_json.dumps(final)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    await asyncio.sleep(0.1)
            return StreamingResponse(sse(), media_type="text/event-stream")

        view = await wait_done()
        if view.state != TaskState.SUCCEEDED:
            error = view.result.error if view.result else view.state.value
            raise HTTPException(502, f"task {view.state.value}: {error}")
        output = view.result.output or ""
        return {
            "id": completion_id, "object": "chat.completion",
            "created": created, "model": model_name,
            "choices": [{"index": 0, "message":
                         {"role": "assistant", "content": output},
                         "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": view.result.prompt_tokens or 0,
                "completion_tokens": view.result.output_tokens or 0,
                "total_tokens": (view.result.prompt_tokens or 0) +
                                (view.result.output_tokens or 0)},
        }

    # ------------------------------------------------------------ model mirror

    @app.get("/models/{artifact}")
    async def model_artifact(artifact: str):
        """LAN GGUF mirror: serve model files cached under
        <data_dir>/models so joining nodes avoid WAN pulls."""
        if "/" in artifact or ".." in artifact:
            raise HTTPException(400, "bad artifact name")
        path = server.config.node.data_dir / "models" / artifact
        if not path.exists():
            raise HTTPException(404, "artifact not cached on this server")
        return FileResponse(path)

    # ---------------------------------------------------------------- web dash

    @app.get("/dash", response_class=HTMLResponse)
    async def web_dash() -> str:
        from pathlib import Path
        page = (Path(__file__).resolve().parents[2] / "client" / "dashboard"
                / "index.html")
        if not page.exists():
            raise HTTPException(404, "dashboard not built")
        return page.read_text()
