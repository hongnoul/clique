"""REST API of the server node — implemented route registrars.

Core routes (register, tasks, nodes, clusters, clique, stats, join
assets) live in ``scheduler/server.py``. This module adds the extended
surface: sessions, suggestions, vcs, kick, ``/v1/server/clear``, and the
OpenAI-compatible ``/v1/chat/completions`` adapter.

Auth: op-gated routes require ``Authorization: Bearer <token>`` from a
registered node.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from common.errors import (
    NodeUnavailableError,
    SessionConflictError,
)
from common.think import StreamSplitter, split_think
from common.types import ChatMessage, TaskRequest, TaskState

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
    """Attach sessions/suggestions/vcs/chat routes."""

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
        _actor(server, request)
        try:
            server.sessions.get(session_id)
        except KeyError:
            raise HTTPException(404, "no such session")
        try:
            session = server.sessions.migrate(
                session_id, body["to_node"], body.get("reason", ""))
        except (NodeUnavailableError, SessionConflictError) as e:
            raise HTTPException(409, str(e))
        await server.events.publish("session.migrated",
                                    session.model_dump(mode="json"))
        return session.model_dump(mode="json")

    @app.delete("/v1/sessions")
    async def clear_sessions() -> dict:
        """Remove every session and its stored turns. Dashboard clear-all."""
        return await server.clear_sessions()

    @app.post("/v1/sessions/clear")
    async def clear_sessions_post() -> dict:
        return await server.clear_sessions()

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict:
        """Remove one session and its conversation from the server."""
        if not await server.delete_session(session_id):
            raise HTTPException(404, "no such session")
        return {"deleted": True, "closed": True}

    @app.post("/v1/sessions/{session_id}/delete")
    async def delete_session_post(session_id: str) -> dict:
        if not await server.delete_session(session_id):
            raise HTTPException(404, "no such session")
        return {"deleted": True, "closed": True}

    @app.post("/v1/nodes/{node_id}/kick")
    async def kick(node_id: str, request: Request) -> dict:
        _actor(server, request)
        info = server.registry.get(node_id)
        if info is None:
            raise HTTPException(404, "no such node")
        ws = server.conns.get(node_id)
        if ws is not None:
            try:
                await ws.close(code=4403)
            except Exception:
                pass
        task_age = (server.router.task_age_s(info.current_task_id)
                   if info.current_task_id else 0.0)
        server.registry.remove(node_id, task_age)
        for tid in server.router.on_node_lost(node_id):
            pass  # requeued
        server.tokens = {t: n for t, n in server.tokens.items() if n != node_id}
        await server.events.publish("node.kicked", {"node_id": node_id})
        return {"kicked": node_id}

    # ---------------------------------------------------------------- server

    @app.post("/v1/server/clear")
    async def clear(request: Request) -> dict:
        """Wipe sessions, the task queue, ledger, and workspaces.

        Nodes stay joined. Auth-gated like shutdown/kick.
        """
        actor = _actor(server, request)
        return await server.clear_data(actor)

    @app.post("/v1/server/shutdown")
    async def shutdown(body: dict, request: Request) -> dict:
        """Stop the server -- from any node, not just its own machine.
        Refuses (409) with the list of active tasks unless confirm=true,
        so a caller (the CLI) can warn and ask before anything is killed."""
        actor = _actor(server, request)
        active = await server.shutdown_active_tasks()
        if active and not body.get("confirm"):
            raise HTTPException(409, {
                "error": "nodes are actively running tasks",
                "active": active,
            })
        await server.events.publish("server.shutdown", {"actor": actor})
        asyncio.create_task(server.shutdown_now())
        return {"shutting_down": True, "active_tasks_killed": len(active)}

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
            sha = server.vcs.rollback(body["sha"], actor)
        except KeyError:
            raise HTTPException(404, "unknown commit sha")
        return {"sha": sha}

    @app.post("/v1/vcs/snapshot")
    async def vcs_snapshot(body: dict, request: Request) -> dict:
        actor = _actor(server, request)
        sha = server.vcs.snapshot(body.get("message", "manual snapshot"), actor)
        return {"sha": sha}

    # ----------------------------------------------- OpenAI-compat completions

    @app.get("/v1/models")
    async def list_models() -> dict:
        """OpenAI-compatible model listing for spawn backends (e.g. jcode).

        Advertises the auto aliases plus one entry per live cluster so
        clients can discover routable model names without prior knowledge.
        """
        ids: list[str] = ["clique", "auto"]
        seen: set[str] = set(ids)
        for c in server.registry.list_clusters():
            for candidate in (c.cluster_key, c.model.family):
                if candidate and candidate not in seen:
                    ids.append(candidate)
                    seen.add(candidate)
        return {"object": "list",
                "data": [{"id": mid, "object": "model",
                          "created": 0, "owned_by": "clique"}
                         for mid in ids]}

    def _node_family(node_id: str | None) -> str | None:
        if not node_id:
            return None
        info = server.registry.get(node_id)
        return info.model.family if info and info.model else None

    def _finish_reason(view) -> str:
        if view.state == TaskState.SUCCEEDED and view.result:
            out_tokens = view.result.output_tokens
            limit = view.request.max_output_tokens
            if out_tokens and limit and out_tokens >= limit:
                return "length"
        return "stop"  # terminal non-success surfaces via HTTP error instead

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(body: dict) -> dict | StreamingResponse:
        """Adapter: wraps a TaskRequest (+ optional session) so existing
        OpenAI-client tools can point at the clique as a provider.

        Reasoning-model output (``...</think>answer``) is split
        server-side: chain-of-thought streams as DeepSeek-style
        ``reasoning_content`` deltas while ``content`` stays clean. Raw
        text remains available on /dash and partial_output.
        """
        messages = body.get("messages", [])
        if not messages:
            raise HTTPException(422, "messages required")
        session_id = body.get("session_id") or body.get("user")
        sid = session_id if session_id and session_id.startswith("s-") else None
        if sid:
            last_user = next(
                (m.get("content", "") for m in reversed(messages)
                 if m.get("role", "user") == "user"),
                messages[-1].get("content", ""))
            prompt = last_user
            chat_messages = None
        else:
            prompt = "\n".join(
                f"{m.get('role', 'user')}: {m.get('content', '')}"
                for m in messages)
            chat_messages = [
                ChatMessage(role=m.get("role", "user"),
                            content=m.get("content", ""))
                for m in messages]
        request = TaskRequest(
            prompt=prompt,
            messages=chat_messages,
            session_id=sid,
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
                """Event-driven: wake on task.progress, no fixed poll.

                Subscribes to the per-task topic fed by the agent WS
                PROGRESS handler. A short-timeout queue wait (0.5s)
                re-checks router state each loop, so a missed event or
                a fast terminal task can never hang the stream.
                """
                import json as _json
                key, q = server.events.subscribe(f"task:{task_id}")
                splitter = StreamSplitter()

                def chunk_for(delta: dict, finish: str | None = None) -> str:
                    payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created, "model": model_name,
                        "choices": [{"index": 0, "delta": delta,
                                     "finish_reason": finish}]}
                    return f"data: {_json.dumps(payload)}\n\n"
                try:
                    while True:
                        view = server.router.get_task(task_id)
                        partial = server.progress.get(task_id, "")
                        done = view and view.state in (
                            TaskState.SUCCEEDED, TaskState.FAILED,
                            TaskState.CANCELLED, TaskState.EXPIRED)
                        if done and view.state == TaskState.SUCCEEDED and \
                                view.result and view.result.output:
                            partial = view.result.output
                        if view is not None and not splitter.locked:
                            splitter.set_family(
                                _node_family(view.assigned_node))
                        r_delta, c_delta = splitter.feed(
                            partial, done=bool(done))
                        if r_delta:
                            yield chunk_for({"reasoning_content": r_delta})
                        if c_delta:
                            yield chunk_for({"content": c_delta})
                        if done:
                            yield chunk_for({}, finish=_finish_reason(view))
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            await asyncio.wait_for(q.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            pass  # re-check router state (missed-event guard)
                finally:
                    server.events.unsubscribe(key)
            return StreamingResponse(
                sse(), media_type="text/event-stream",
                headers={"x-clique-task-id": task_id})

        view = await wait_done()
        if view.state != TaskState.SUCCEEDED:
            error = view.result.error if view.result else view.state.value
            raise HTTPException(502, f"task {view.state.value}: {error}")
        output = view.result.output or ""
        reasoning, content = split_think(output)
        message: dict = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        return {
            "id": completion_id, "object": "chat.completion",
            "created": created, "model": model_name,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": _finish_reason(view)}],
            "usage": {
                "prompt_tokens": view.result.prompt_tokens or 0,
                "completion_tokens": view.result.output_tokens or 0,
                "total_tokens": (view.result.prompt_tokens or 0) +
                                (view.result.output_tokens or 0)},
        }

    # ------------------------------------------------------------ code tasks

    @app.post("/v1/code/tasks")
    async def code_submit(body: dict) -> dict:
        """Submit a CODE_EDIT task: {prompt, code: CodeTaskSpec, ...}.

        Server builds the diff-request prompt, creates the workspace on
        assignment via submit_task, and verifies node output before commit.
        """
        from common.types import CodeTaskSpec, TaskType
        body = dict(body)
        body["task_type"] = TaskType.CODE_EDIT.value
        if "idempotency_key" not in body:
            body["idempotency_key"] = uuid.uuid4().hex
        request = TaskRequest.model_validate(body)
        if request.code is None:
            raise HTTPException(422, "code spec required")
        CodeTaskSpec.model_validate(request.code.model_dump())
        return {"task_id": await server.submit_task(request)}

    @app.get("/v1/code/tasks/{task_id}/diff")
    async def code_diff(task_id: str) -> dict:
        view = server.router.get_task(task_id)
        if view is None:
            raise HTTPException(404, "no such task")
        result = view.result
        return {"task_id": task_id,
                "patch": result.patch if result else None,
                "applied_sha": result.applied_sha if result else None,
                "partial_output": server.progress.get(task_id, "")}

    @app.get("/v1/code/tasks/{task_id}/tests")
    async def code_tests(task_id: str) -> dict:
        view = server.router.get_task(task_id)
        if view is None:
            raise HTTPException(404, "no such task")
        result = view.result
        return {"task_id": task_id,
                "test_report": result.test_report if result else None,
                "state": view.state.value}

    @app.post("/v1/code/race")
    async def code_race(body: dict) -> dict:
        """Fan out one CodeTaskSpec to N parallel tasks (replica race).

        Body: {prompt, code, fanout=2}. Each member gets idempotency_key
        ``race:<race_id>:<i>``. First harness-accepted patch wins; the
        server cancels siblings on accept. Returns {race_id, task_ids}.
        """
        import copy
        prompt = body.get("prompt", "")
        spec = body.get("code")
        if not prompt or not spec:
            raise HTTPException(422, "prompt and code required")
        fanout = max(1, min(int(body.get("fanout", 2)), 8))
        race_id = uuid.uuid4().hex[:12]
        task_ids = []
        for i in range(fanout):
            member = copy.deepcopy(body)
            member["task_type"] = "code_edit"
            member["idempotency_key"] = f"race:{race_id}:{i}"
            request = TaskRequest.model_validate(member)
            task_ids.append(await server.submit_task(request))
        server.race_groups[race_id] = task_ids
        return {"race_id": race_id, "task_ids": task_ids}

    @app.get("/v1/ledger")
    async def ledger() -> dict:
        """Accepted-work accounting: totals, per-node earnings, states."""
        return server.ledger.summary()

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

    _assets = (Path(__file__).resolve().parents[2] / "client" / "dashboard"
               / "assets")
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/dash", response_class=HTMLResponse)
    async def web_dash() -> str:
        from pathlib import Path
        page = (Path(__file__).resolve().parents[2] / "client" / "dashboard"
                / "index.html")
        if not page.exists():
            raise HTTPException(404, "dashboard not built")
        return page.read_text()
