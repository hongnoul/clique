"""Python SDK for the server node API (full implementation)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx

from common.types import Cluster, NodeInfo, TaskRequest, TaskState, TaskView


class CliqueClient:
    def __init__(self, base_url: str, timeout_s: float = 30.0,
                 token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self.token = token
        self.node_id: str | None = None
        self._data_dir: Path | None = None

    @classmethod
    async def discover(cls, timeout_s: float = 5.0) -> "CliqueClient":
        from node.discovery import find_server
        ann = await find_server(timeout_s=timeout_s)
        if ann is None:
            raise ConnectionError("no clique server found on this network")
        return cls(f"http://{ann.api_address}")

    async def _authed_request(self, method: str, url: str,
                              body: dict | None = None,
                              **params) -> dict | list:
        """One request with silent re-auth on 401 (server restart).

        The server keeps tokens in memory only, so a restart invalidates
        every cached token. On 401 this drops the stale cache, registers
        fresh exactly once, and retries. The caller never sees the 401.
        """
        async def _once() -> "httpx.Response":
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=self._timeout) as c:
                fn = {"GET": c.get, "POST": c.post,
                      "DELETE": c.delete}[method]
                kw: dict = {"headers": self._headers()}
                if method == "GET":
                    kw["params"] = params or None
                elif method == "POST":
                    kw["json"] = body
                return await fn(f"{self.base_url}{url}", **kw)

        r = await _once()
        if (r.status_code == 401 and self.token
                and not url == "/v1/register"):
            self.token = None  # force fresh register, ignore stale cache
            try:
                if self._data_dir is not None:
                    (self._data_dir / "node.token").unlink(missing_ok=True)
            except Exception:
                pass
            await self.authenticate(self._data_dir)
            r = await _once()
        r.raise_for_status()
        return r.json()

    async def _get(self, url: str, **params) -> dict | list:
        return await self._authed_request("GET", url, **params)

    async def _post(self, url: str, body: dict | None = None) -> dict:
        return await self._authed_request("POST", url, body)  # type: ignore[return-value]

    async def _delete(self, path: str) -> dict:
        return await self._authed_request("DELETE", path)  # type: ignore[return-value]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def authenticate(self, data_dir: Path | None = None) -> str:
        """Register this device's keypair with the server to obtain a node
        token (required for op-gated routes). Reuses the agent identity
        when one exists in data_dir.

        Seamless: the token is cached at ``<data_dir>/node.token`` keyed
        by server URL, so repeat CLI calls reuse it instead of minting a
        new token per invocation. The user never sees or pastes a token.
        """
        from common import protocol
        from common.config import DEFAULT_DIR, generate_or_load_keypair
        import socket
        d = data_dir or DEFAULT_DIR
        self._data_dir = d
        cache = d / "node.token"
        if self.token is None and cache.exists():
            try:
                import json as _json
                saved = _json.loads(cache.read_text())
                if saved.get("server") == self.base_url and saved.get("token"):
                    self.token = saved["token"]
                    self.node_id = saved.get("node_id")
                    return self.token
            except Exception:
                pass  # corrupt cache: fall through to fresh register
        pub, seed = generate_or_load_keypair(d)
        name = socket.gethostname().split(".")[0]
        body = {"display_name": name, "public_key": pub,
                "signature": protocol.sign_payload(name.encode(), seed),
                "role": "client"}
        data = await self._post("/v1/register", body)
        self.token = data["token"]
        self.node_id = data["node_id"]
        try:
            import json as _json
            d.mkdir(parents=True, exist_ok=True)
            cache.write_text(_json.dumps(
                {"server": self.base_url, "token": self.token,
                 "node_id": self.node_id}))
            cache.chmod(0o600)
        except Exception:
            pass  # cache is best-effort; in-memory token still works
        return self.token

    # -- tasks ---------------------------------------------------------------

    async def submit(self, prompt: str, *, task_type: str = "chat",
                     model_hint: str | None = None,
                     session_id: str | None = None,
                     workspace_id: str | None = None,
                     max_output_tokens: int = 1024) -> str:
        request = TaskRequest(
            prompt=prompt, model_hint=model_hint, session_id=session_id,
            workspace_id=workspace_id,
            max_output_tokens=max_output_tokens,
            idempotency_key=uuid.uuid4().hex,
        )
        body = request.model_dump(mode="json")
        body["task_type"] = task_type
        return (await self._post("/v1/tasks", body))["task_id"]

    async def task(self, task_id: str) -> dict:
        return await self._get(f"/v1/tasks/{task_id}")  # includes partial_output

    async def wait(self, task_id: str, poll_s: float = 0.15,
                   timeout_s: float = 600.0) -> TaskView:
        """Block until terminal. For live tokens use stream() instead."""
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

    async def stream(self, task_id: str, poll_s: float = 0.1,
                     timeout_s: float = 600.0):
        """Yield (partial_output_delta, done_view) as tokens arrive.

        Polls GET /v1/tasks/{id} (server accumulates agent WS progress
        into partial_output) and yields each new slice the moment it
        appears. The final yield carries the terminal TaskView; earlier
        yields carry None as the view. Usage:
            async for delta, done in client.stream(tid):
                if delta: print(delta, end="", flush=True)
                if done is not None: view = done
        """
        deadline = asyncio.get_event_loop().time() + timeout_s
        shown = 0
        while True:
            data = await self.task(task_id)
            partial = data.get("partial_output", "") or ""
            if len(partial) > shown:
                yield partial[shown:], None
                shown = len(partial)
            view = TaskView.model_validate(data)
            if view.state in (TaskState.SUCCEEDED, TaskState.FAILED,
                              TaskState.CANCELLED, TaskState.EXPIRED):
                # terminal output may exceed streamed partial (buffered
                # tail or code-task result): emit the remainder once.
                out = (view.result.output or "") if view.result else ""
                if len(out) > shown:
                    yield out[shown:], None
                yield "", view
                return
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"task {task_id} still {view.state.value}")
            await asyncio.sleep(poll_s)

    async def stream_ws(self, task_id: str, timeout_s: float = 600.0):
        """Yield (delta, done_view) over /ws/tasks/{id}, no polling.

        Push model: server publishes task.progress per agent flush and
        task.finished on commit. Drops to one HTTP GET per event (to
        read accumulated partial_output / terminal view), zero traffic
        while idle. Falls back to stream() polling when the WS drops.
        Same yield contract as stream().
        """
        import json as _json

        import websockets as _ws

        ws_url = self.base_url.replace("http", "ws", 1) + f"/ws/tasks/{task_id}"
        wskw: dict = {}
        if ws_url.startswith("wss"):
            from common.tls import client_ssl_context
            wskw["ssl"] = client_ssl_context()
        deadline = asyncio.get_event_loop().time() + timeout_s
        shown = 0
        try:
            async with _ws.connect(ws_url, max_size=None, **wskw) as sock:
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            sock.recv(),
                            timeout=max(0.1, deadline -
                                        asyncio.get_event_loop().time()))
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"task {task_id} timed out on WS")
                    try:
                        msg = _json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if msg.get("event") == "task.progress":
                        data = await self.task(task_id)
                        partial = data.get("partial_output", "") or ""
                        if len(partial) > shown:
                            yield partial[shown:], None
                            shown = len(partial)
                    elif msg.get("event") == "task.finished":
                        data = await self.task(task_id)
                        view = TaskView.model_validate(data)
                        out = (view.result.output or "") if view.result else ""
                        if len(out) > shown:
                            yield out[shown:], None
                        yield "", view
                        return
                    if asyncio.get_event_loop().time() > deadline:
                        raise TimeoutError(f"task {task_id} timed out on WS")
        except (OSError, _ws.WebSocketException):
            async for delta, done in self.stream(task_id, timeout_s=max(
                    1.0, deadline - asyncio.get_event_loop().time())):
                yield delta, done
            return
        # server closes the socket right after task.finished (or when a
        # late subscriber asks about an already-terminal task): one final
        # HTTP settle, then fall back to polling only if still running.
        data = await self.task(task_id)
        view = TaskView.model_validate(data)
        if view.state in (TaskState.SUCCEEDED, TaskState.FAILED,
                          TaskState.CANCELLED, TaskState.EXPIRED):
            out = (view.result.output or "") if view.result else ""
            if len(out) > shown:
                yield out[shown:], None
            yield "", view
            return
        shown_data = await self.task(task_id)
        already = len(shown_data.get("partial_output", "") or "")
        shown = max(shown, already)
        skip = shown
        async for delta, done in self.stream(task_id, timeout_s=max(
                1.0, deadline - asyncio.get_event_loop().time())):
            # stream() replays from zero; drop what WS already delivered
            if delta and skip > 0:
                if len(delta) <= skip:
                    skip -= len(delta)
                    delta = ""
                else:
                    delta = delta[skip:]
                    skip = 0
            yield delta, done

    async def cancel(self, task_id: str) -> bool:
        return (await self._post(f"/v1/tasks/{task_id}/cancel"))["cancelled"]

    async def code_submit(self, prompt: str, files: dict[str, str],
                          test_cmd: list[str] | None = None,
                          allowed_paths: list[str] | None = None,
                          use_tools: bool = False,
                          workspace_id: str | None = None,
                          **kw) -> str:
        """Submit a CODE_EDIT task; server verifies the diff before commit."""
        body: dict = {"prompt": prompt,
                      "workspace_id": workspace_id,
                      "code": {"files": files,
                               "test_cmd": test_cmd or ["pytest", "-q"],
                               "allowed_paths": allowed_paths or [],
                               "use_tools": use_tools}}
        body.update(kw)
        return (await self._post("/v1/code/tasks", body))["task_id"]

    async def code_diff(self, task_id: str) -> dict:
        return await self._get(f"/v1/code/tasks/{task_id}/diff")

    async def code_tests(self, task_id: str) -> dict:
        return await self._get(f"/v1/code/tasks/{task_id}/tests")

    async def code_race(self, prompt: str, files: dict[str, str],
                        fanout: int = 2,
                        test_cmd: list[str] | None = None) -> dict:
        return await self._post("/v1/code/race", {
            "prompt": prompt,
            "code": {"files": files,
                     "test_cmd": test_cmd or ["pytest", "-q"]},
            "fanout": fanout})

    async def ledger(self) -> dict:
        return await self._get("/v1/ledger")  # type: ignore[return-value]

    # -- clique views ----------------------------------------------------------

    async def nodes(self) -> list[NodeInfo]:
        return [NodeInfo.model_validate(n) for n in await self._get("/v1/nodes")]

    async def clusters(self) -> list[Cluster]:
        return [Cluster.model_validate(c) for c in await self._get("/v1/clusters")]

    async def clique(self) -> dict:
        return await self._get("/v1/clique")  # type: ignore[return-value]

    async def stats(self) -> dict:
        return await self._get("/v1/stats")  # type: ignore[return-value]

    # -- sessions --------------------------------------------------------------

    async def create_session(self, cluster_key: str = "") -> dict:
        return await self._post("/v1/sessions", {"cluster_key": cluster_key})

    async def sessions(self) -> list[dict]:
        return await self._get("/v1/sessions")  # type: ignore[return-value]

    async def session(self, session_id: str) -> dict:
        return await self._get(f"/v1/sessions/{session_id}")

    async def ensure_chat_session(self, cluster_key: str = "", *,
                                  store_path: Path | None = None,
                                  reset: bool = False) -> str:
        """Reuse the sticky CLI session for this server, or create one.

        Session ids are stored per server URL in ``~/.clique/cli-sessions.json``
        so consecutive ``clique submit`` calls share transcript context.
        """
        from common.config import DEFAULT_DIR
        path = store_path or (DEFAULT_DIR / "cli-sessions.json")
        store: dict = {}
        if path.exists():
            try:
                store = json.loads(path.read_text())
            except json.JSONDecodeError:
                store = {}
        key = self.base_url
        entry = store.get(key) or {}
        sid = entry.get("session_id") if isinstance(entry, dict) else None
        if sid and not reset:
            try:
                await self.session(sid)
                return sid
            except httpx.HTTPStatusError:
                pass
        if not self.token:
            await self.authenticate()
        created = await self.create_session(cluster_key)
        sid = created["session_id"]
        store[key] = {"session_id": sid, "cluster_key": cluster_key}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, indent=2))
        return sid

    def forget_chat_session(self, session_id: str | None = None,
                            store_path: Path | None = None) -> None:
        """Drop the sticky CLI session if it matches ``session_id`` (or always)."""
        from common.config import DEFAULT_DIR
        path = store_path or (DEFAULT_DIR / "cli-sessions.json")
        if not path.exists():
            return
        try:
            store = json.loads(path.read_text())
        except json.JSONDecodeError:
            return
        entry = store.get(self.base_url) or {}
        if session_id is None or entry.get("session_id") == session_id:
            store.pop(self.base_url, None)
            path.write_text(json.dumps(store, indent=2))

    async def migrate_session(self, session_id: str, to_node: str,
                              reason: str = "") -> dict:
        return await self._post(f"/v1/sessions/{session_id}/migrate",
                                {"to_node": to_node, "reason": reason})

    async def close_session(self, session_id: str) -> dict:
        return await self._delete(f"/v1/sessions/{session_id}")

    # -- admin -----------------------------------------------------------------

    async def kick(self, node_id: str) -> dict:
        return await self._post(f"/v1/nodes/{node_id}/kick")

    async def server_shutdown(self, confirm: bool = False) -> dict:
        """Raises httpx.HTTPStatusError(409) with response.json()["detail"]
        = {"active": [...]} if nodes have active tasks and confirm=False."""
        return await self._post("/v1/server/shutdown", {"confirm": confirm})

    # -- suggestions & vcs -----------------------------------------------------

    async def suggestions(self) -> list[dict]:
        return await self._get("/v1/suggestions")  # type: ignore[return-value]

    async def dismiss_suggestion(self, suggestion_id: str) -> dict:
        return await self._post(f"/v1/suggestions/{suggestion_id}/dismiss")

    async def vcs_history(self, limit: int = 50) -> list[dict]:
        return await self._get("/v1/vcs/history", limit=limit)  # type: ignore

    async def vcs_diff(self, a: str, b: str) -> str:
        return (await self._get("/v1/vcs/diff", a=a, b=b))["diff"]  # type: ignore

    async def vcs_rollback(self, sha: str) -> dict:
        return await self._post("/v1/vcs/rollback", {"sha": sha})

    # -- live workspaces (realtime collab) -----------------------------------

    async def workspace_create(self, files: dict[str, str] | None = None,
                               workspace_id: str | None = None) -> dict:
        return await self._post("/v1/workspaces",
                                {"files": files or {},
                                 "workspace_id": workspace_id})

    async def workspaces(self) -> list[dict]:
        return await self._get("/v1/workspaces")  # type: ignore[return-value]

    async def workspace(self, workspace_id: str) -> dict:
        return await self._get(f"/v1/workspaces/{workspace_id}")

    async def workspace_file(self, workspace_id: str, path: str) -> dict:
        return await self._get(f"/v1/workspaces/{workspace_id}/file",
                               path=path)

    async def workspace_patch(self, workspace_id: str, path: str,
                              ops: list[dict],
                              base_version: int = 0) -> dict:
        """One-shot sequenced write to the live workspace (socket VCS).

        Stale ``base_version`` is rebased server-side, never rejected.
        Returns the applied event: {version, seq, ops, rebased, ...}.
        """
        return await self._post(
            f"/v1/workspaces/{workspace_id}/patch",
            {"path": path, "base_version": base_version, "ops": ops})

    async def workspace_write(self, workspace_id: str, path: str,
                              text: str) -> dict:
        """Replace one file's full content (convenience over workspace_patch).

        Reads head version first so history records an honest base.
        """
        try:
            st = await self.workspace_file(workspace_id, path)
            base = int(st.get("version", 0))
        except httpx.HTTPStatusError:
            base = 0  # new file in the workspace
        return await self.workspace_patch(
            workspace_id, path,
            [{"op": "replace_file", "text": text}], base_version=base)

    async def workspace_flush(self, workspace_id: str) -> dict:
        return await self._post(f"/v1/workspaces/{workspace_id}/flush")

    async def workspace_export(self, workspace_id: str,
                               paths: list[str] | None = None) -> dict:
        """Export full file contents + git provenance for docs-sync.

        Dogfood agents draft in the live workspace, export the bundle,
        and open a PR from a credentialed machine. Optional path filter.
        """
        params: dict = {}
        if paths:
            params["paths"] = ",".join(paths)
        return await self._get(
            f"/v1/workspaces/{workspace_id}/export", **params)

    async def workspace_watch(self, workspace_id: str,
                              timeout_s: float = 0.0):
        """Yield realtime workspace events over /ws/workspace/{id}.

        First event is the snapshot, then deltas/presence as they land.
        ``timeout_s`` > 0 stops the generator after that many seconds of
        silence (useful for scripted verification); 0 waits forever.
        """
        import websockets as _ws
        url = self.base_url.replace("http", "ws", 1) \
            + f"/ws/workspace/{workspace_id}"
        if self.token:
            url += f"?token={self.token}"
        wskw: dict = {}
        if url.startswith("wss"):
            import ssl
            wskw["ssl"] = ssl.create_default_context()
        async with _ws.connect(url, max_size=None, **wskw) as sock:
            while True:
                if timeout_s > 0:
                    try:
                        raw = await asyncio.wait_for(sock.recv(), timeout_s)
                    except asyncio.TimeoutError:
                        return
                else:
                    raw = await sock.recv()
                yield json.loads(raw)

    async def workspace_history(self, workspace_id: str,
                                path: str | None = None,
                                limit: int = 50) -> list[dict]:
        params: dict = {"limit": limit}
        if path:
            params["path"] = path
        return await self._get(  # type: ignore[return-value]
            f"/v1/workspaces/{workspace_id}/history", **params)

    async def workspace_commits(self, workspace_id: str,
                                limit: int = 20) -> list[dict]:
        return await self._get(  # type: ignore[return-value]
            f"/v1/workspaces/{workspace_id}/commits", limit=limit)
