"""Server node (full implementation).

FastAPI app exposing:
- POST /v1/register           signed node registration -> token
- WS   /ws/agent?token=       agent channel (heartbeat/assign/result)
- POST /v1/tasks              submit task
- GET  /v1/tasks/{id}         task view (state + result)
- POST /v1/tasks/{id}/cancel
- GET  /v1/tasks              list
- GET  /v1/nodes, /v1/clusters, /v1/clique, /v1/stats
- extended surface (scheduler/api/rest.py): sessions, suggestions,
  vcs, kick, /v1/server/clear, /v1/chat/completions, /dash
- WS firehose (scheduler/api/ws.py): /ws/events, /ws/sessions/{id}

A background loop schedules queued tasks onto ready agent connections,
expires leases, marks stale nodes offline,
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
from common.errors import SessionConflictError
from common.types import (
    ChatMessage,
    ModelSpec,
    NodeRole,
    NodeStatus,
    ResourceSnapshot,
    TaskRequest,
    TaskResult,
    TaskState,
    TaskType,
    node_id_from_public_key,
    utcnow,
)
from scheduler.api.rest import register_extended_routes
from scheduler.api.workspace_routes import register_workspace_routes
from scheduler.api.ws import EventBroadcaster, register_ws_routes
from scheduler.context_store import ContextStore, summary_max_output_tokens
from scheduler.registry import Registry
from scheduler.router import Router
from scheduler.sessions import SessionManager
from scheduler.suggestions import SuggestionEngine
from scheduler.vcs import VcsService
from scheduler.workspace import WorkspaceService

log = logging.getLogger("clique.server")


def _server_sha() -> str:
    """Short git SHA of the running server tree (mismatch detection).

    Best-effort: returns "unknown" when git is unavailable (tarball
    installs) so the field is always present but never blocks."""
    import subprocess
    from pathlib import Path
    with contextlib.suppress(Exception):
        root = Path(__file__).resolve().parents[1]
        sha = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            timeout=5, stderr=subprocess.DEVNULL).decode().strip()
        if sha:
            return sha
    return "unknown"


def _public_url() -> str | None:
    """Public HTTPS URL of this server, when a tunnel is up.

    True zero-context onboarding: a joiner needs no tailscale, no VPN,
    no LAN, no account. Sources, first hit wins:
      1. $CLIQUE_PUBLIC_URL (explicit, e.g. a named domain)
      2. ~/.clique/public_url (written by the tunnel unit, e.g.
         cloudflared quick tunnel: clique-tunnel.service)
    """
    import os
    from pathlib import Path
    url = os.environ.get("CLIQUE_PUBLIC_URL", "").strip()
    if url:
        return url.rstrip("/")
    with contextlib.suppress(Exception):
        text = (Path.home() / ".clique" / "public_url").read_text().strip()
        if text.startswith("http"):
            return text.rstrip("/")
    return None


_JOIN_SH_TEMPLATE = """#!/bin/sh
# clique join: one line, no token, no GitHub, no ssh key.
#   curl -fsSL __CLIQUE_SERVER__/join.sh | sh
# Source comes from __CLIQUE_SERVER__/app.tgz (this server's own tree).
# Clique auth happens after install via signed registration
# (node keypair -> bearer token), never as a pasted secret.
set -eu
CLIQUE_SERVER="__CLIQUE_SERVER__"
INSTALL_DIR="${CLIQUE_HOME:-$HOME/.clique/app}"
BIN_DIR="${CLIQUE_BIN:-$HOME/.local/bin}"

main() {
    echo ""
    echo "   ( )--( )--( )"
    echo "    |    |    |     clique installer"
    echo "   ( )--( )--( )   idle laptops, one pool"
    echo ""

    OS="$(uname -s)"
    case "$OS" in
        Linux)  os="linux" ;;
        Darwin) os="macos" ;;
        *)      os="$OS" ;;
    esac
    ARCH="$(uname -m)"
    case "$ARCH" in
        x86_64|amd64)   arch="x86_64" ;;
        aarch64|arm64)  arch="aarch64" ;;
        *)              arch="$ARCH" ;;
    esac
    log "detected ${os}/${arch}"

    need curl
    need tar

    PY=""
    for cand in python3.16 python3.15 python3.14 python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'; then
                PY="$cand"; break
            fi
        fi
    done
    [ -n "$PY" ] || err "python 3.11+ required (have: $(command -v python3 || echo none))"
    log "using $($PY --version 2>&1)"

    case "$INSTALL_DIR" in ""|"/"|"$HOME"|"$HOME/") err "refusing to unpack into '$INSTALL_DIR'";; esac

    log "fetching app bundle..."
    tmpfile="${TMPDIR:-/tmp}/clique-app-$$.tgz"
    rm -f "$tmpfile"
    trap 'rm -f "$tmpfile"' EXIT INT TERM
    if ! curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 "$CLIQUE_SERVER/app.tgz" -o "$tmpfile"; then
        err "download failed from $CLIQUE_SERVER/app.tgz (is the server up?)"
    fi
    if ! od -An -N2 -tx1 "$tmpfile" | tr -d ' \n' | grep -q "^1f8b"; then
        head -c 300 "$tmpfile" | tr -d '\\0' | head -n 5 || true
        err "app bundle is not gzip (wrong server?)"
    fi
    log "unpacking..."
    rm -rf "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    tar -xz -C "$INSTALL_DIR" -f "$tmpfile"
    rm -f "$tmpfile"
    trap - EXIT INT TERM
    [ -f "$INSTALL_DIR/pyproject.toml" ] || err "bundle is not a clique checkout"
    log "creating virtualenv..."
    "$PY" -m venv "$INSTALL_DIR/.venv"
    log "installing clique (this takes a minute)..."
    "$INSTALL_DIR/.venv/bin/pip" install -q -U pip
    "$INSTALL_DIR/.venv/bin/pip" install -q -e "$INSTALL_DIR"
    log "linking..."
    mkdir -p "$BIN_DIR"
    for cmd in clique clique-agent clique-server clique-mcp; do
        ln -sf "$INSTALL_DIR/.venv/bin/$cmd" "$BIN_DIR/$cmd"
    done

    log "installed clique to ${BIN_DIR}/clique (server: ${CLIQUE_SERVER})"

    # Headless MCP setup: register clique-mcp with every local agent
    # harness (Claude Code, Codex, Cursor, ...), pinned to this server.
    # Idempotent and skips harnesses that are not installed.
    "$BIN_DIR/clique" mcp install --url "$CLIQUE_SERVER" || \
        warn "mcp install failed (rerun later: clique mcp install --url $CLIQUE_SERVER)"

    case ":${PATH}:" in
        *":${BIN_DIR}:"*) ;;
        *)
            echo ""
            warn "${BIN_DIR} is not in your PATH"
            echo "  add it to your shell config:"
            echo ""
            echo "    export PATH=\\"${BIN_DIR}:\\$PATH\\""
            echo ""
            ;;
    esac

    "$BIN_DIR/clique" --help >/dev/null 2>&1 || err "install check failed ('clique --help')"

    echo ""
    log "ready. run 'clique' and press Join (or Host to start a server)."
    echo ""
    echo "  one step:   export CLIQUE_SERVER=$CLIQUE_SERVER"
    echo "              clique            # buttons: Host Join Chat Dashboard"
    echo "  (headless:  clique join --server $CLIQUE_SERVER --runtime echo --param-b 7)"
    echo "  agents:     clique mcp status  # clique tools in Claude Code/Codex/Cursor"
    echo ""
}

log()  { printf '  \\033[32m>\\033[0m %s\\n' "$1"; }
warn() { printf '  \\033[33m!\\033[0m %s\\n' "$1"; }
err()  { printf '  \\033[31mx\\033[0m %s\\n' "$1" >&2; exit 1; }

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        err "requires '$1' — install it first"
    fi
}

main "$@"
"""


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

    def items(self) -> list[tuple[str, WebSocket]]:
        return list(self._ws.items())


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
        self._compacting: set[str] = set()
        self._tick_task: asyncio.Task | None = None

        # extended subsystems (P2/P3)
        db = config.server.db_path
        self.events = EventBroadcaster()
        self.contexts = ContextStore(db)
        self.sessions = SessionManager(db, self.contexts, self.registry)
        self.router.sessions = self.sessions
        self.router.contexts = self.contexts
        self.suggestions = SuggestionEngine(self.registry, self.router)
        self.vcs = VcsService(config.node.data_dir / "state-repo")
        self.live_workspaces = WorkspaceService(
            config.node.data_dir / "live-workspaces")
        self.vcs.register("sessions.json", self.sessions.export_state)
        self.vcs.register("nodes.json", self._export_nodes)
        self.vcs.init()
        # code-edit: canonical user-code repo + ephemeral workspaces.
        # Separate from state-repo (server audit history).
        from scheduler.ledger import Ledger
        from scheduler.workspaces import WorkspaceManager
        self.code_vcs = VcsService(config.node.data_dir / "code-repo")
        self.code_vcs.init()
        self.workspaces = WorkspaceManager(
            config.node.data_dir / "workspaces")
        self.ledger = Ledger(db)
        self.vcs.register("ledger.json", self.ledger.export_state)
        self.race_groups: dict[str, list[str]] = {}  # race_id -> task_ids

        self.app = self._build_app()

    def _export_nodes(self) -> str:
        import json
        return json.dumps(
            [{"node_id": n.node_id, "display_name": n.display_name,
              "role": n.role.value,
              "model": n.model.cluster_key() if n.model else None}
             for n in self.registry.list_nodes()], indent=2, sort_keys=True)

    async def submit_task(self, request: TaskRequest) -> str:
        """Submit + immediate scheduling; shared by REST routes."""
        from common.types import TaskType as _TaskType
        raw_code_prompt = request.prompt  # pre-prompt-build, for session log
        live_snap: dict | None = None
        if request.workspace_id:
            # validate workspace exists; stamp head seq so the agent can
            # detect drift (invalidate msgs carry newer seqs)
            try:
                live_snap = self.live_workspaces.snapshot(request.workspace_id)
            except KeyError:
                raise HTTPException(404, "no such workspace")
            request.workspace_seq = live_snap["seq"]
        if request.task_type == _TaskType.CODE_EDIT and request.code is not None:
            from scheduler.harness import build_code_prompt
            if not request.prompt:
                raise HTTPException(422, "prompt required for code tasks")
            if live_snap is not None:
                # live files win over the client-supplied snapshot: the
                # agent reasons on what collaborators see right now
                merged = dict(request.code.files)
                for path, f in live_snap["files"].items():
                    merged[path] = f["text"]
                request.code.files = merged
            request.prompt = build_code_prompt(request.prompt, request.code)
            # Reasoning models (nemotron et al) spend completion tokens on
            # thinking before the diff: 2048 truncates mid-fence. 6144
            # leaves room for think + a real patch on small-context models.
            request.max_output_tokens = max(request.max_output_tokens, 6144)
        if request.session_id:
            try:
                session = self.sessions.get(request.session_id)
            except KeyError:
                raise HTTPException(404, "no such session")
            if request.record_turns:
                try:
                    self.sessions.append_turn_latest(
                        request.session_id, "user", raw_code_prompt)
                except SessionConflictError as e:
                    raise HTTPException(409, str(e)) from e
            if not request.model_hint and session.cluster_key:
                request.model_hint = session.cluster_key
        try:
            task_id = self.router.submit(request)
        except OverflowError as e:
            raise HTTPException(429, str(e)) from e
        await self.events.publish("task.submitted", {"task_id": task_id})
        await self._schedule_now()
        return task_id

    def _session_window(self, session_id: str, fallback: int = 8192) -> int:
        try:
            session = self.sessions.get(session_id)
        except KeyError:
            return fallback
        candidates = []
        if session.pinned_node:
            candidates.append(session.pinned_node)
        for n in self.registry.list_nodes():
            if n.node_id in candidates:
                continue
            if session.cluster_key and n.model is not None \
                    and n.model.cluster_key().startswith(session.cluster_key):
                candidates.append(n.node_id)
        for nid in candidates:
            node = self.registry.get(nid)
            if node and node.model:
                return node.model.context_window
        return fallback

    def _commit_compaction(self, session_id: str, content: str,
                           covers: list[int]) -> None:
        self.sessions.append_compaction_latest(session_id, content, covers)

    async def _maybe_compact(self, session_id: str,
                             max_output_tokens: int) -> None:
        """Enqueue one summarization job if older turns no longer fit.

        ``max_output_tokens`` is the triggering *chat* turn's cap, used
        only to detect overflow. The job's own generation cap is a
        slice of the model window.
        """
        if session_id in self._compacting:
            return
        window = self._session_window(session_id)
        plan = self.contexts.compaction_plan(
            session_id, window, max_output_tokens)
        if plan is None:
            return
        try:
            session = self.sessions.get(session_id)
        except KeyError:
            return
        self._compacting.add(session_id)
        messages = [ChatMessage.model_validate(m) for m in plan["messages"]]
        prompt = "\n".join(f"{m.role}: {m.content}" for m in messages)
        prompt_tokens = max(1, len(prompt) // 4)
        request = TaskRequest(
            prompt=prompt,
            messages=messages,
            session_id=session_id,
            model_hint=session.cluster_key or None,
            task_type=TaskType.SUMMARIZATION,
            record_turns=False,
            compaction_covers=plan["covers"],
            max_output_tokens=summary_max_output_tokens(
                window, prompt_tokens),
            idempotency_key=(
                f"compact:{session_id}:{plan['covers'][1]}:"
                f"{self.contexts.latest_version(session_id)}"),
        )
        try:
            task_id = self.router.submit(request)
        except OverflowError:
            self._compacting.discard(session_id)
            return
        await self.events.publish(
            "session.compacting",
            {"session_id": session_id, "task_id": task_id,
             "covers": plan["covers"]},
            topic=f"session:{session_id}")
        await self._schedule_now()

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
            with contextlib.suppress(Exception):
                await self.live_workspaces.shutdown_flush()

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
            # one live token per node: prune stale tokens from prior
            # authenticate() calls so the in-memory map does not leak.
            self.tokens = {t: n for t, n in self.tokens.items()
                           if n != node_id}
            self.tokens[token] = node_id
            await self.events.publish("node.joined", {
                "node_id": node_id, "display_name": body["display_name"]})
            return {"node_id": node_id, "token": token,
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
            if cancelled:
                self.workspaces.cleanup(task_id)
                await self.events.publish("task.finished", {
                    "task_id": task_id, "state": "cancelled",
                    "node_id": view.assigned_node},
                    topic=f"task:{task_id}")
            if cancelled and view.assigned_node:
                ws = self.conns.get(view.assigned_node)
                if ws is not None:
                    await ws.send_text(protocol.dumps(protocol.msg_revoke(
                        task_id, view.attempt_id or "", "cancelled by user")))
                self.registry.set_status(
                    view.assigned_node, NodeStatus.READY, current_task_id=None)
            return {"cancelled": cancelled}

        @app.post("/v1/tasks/{task_id}/delete")
        async def delete_task_post(task_id: str) -> dict:
            if not await self.delete_task(task_id):
                raise HTTPException(404, "no such task")
            return {"deleted": True}

        @app.post("/v1/tasks/clear")
        async def clear_queue_post() -> dict:
            return await self.clear_queue()

        @app.delete("/v1/tasks/{task_id}")
        async def delete_task(task_id: str) -> dict:
            if not await self.delete_task(task_id):
                raise HTTPException(404, "no such task")
            return {"deleted": True}

        @app.delete("/v1/tasks")
        async def clear_queue() -> dict:
            return await self.clear_queue()

        @app.get("/v1/tasks")
        async def list_tasks(state: str | None = None, limit: int = 100) -> list[dict]:
            cap = min(max(limit, 1), 1000)
            return [v.model_dump(mode="json")
                    for v in self.router.list_tasks(state, limit=cap)]

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
                    "clusters": [c.model_dump(mode="json")
                                 for c in self.registry.list_clusters()],
                    "protocol_version": protocol.PROTOCOL_VERSION,
                    "server_sha": _server_sha(),
                    "public_url": _public_url()}

        @app.get("/v1/stats")
        async def stats() -> dict:
            s = self.router.queue_stats()
            s["nodes"] = {n.node_id: n.status.value for n in self.registry.list_nodes()}
            s["ledger"] = self.ledger.summary()
            return s

        # ------------------------------------------------ headless curl flow
        # Zero-install TUI for monitor-less nodes: pure curl, or
        # curl + system python3 (stdlib only). No pip, no clone.

        @app.get("/", response_class=PlainTextResponse)
        async def index_txt(request: Request) -> str:
            base = str(request.base_url).rstrip("/")
            pub = _public_url()
            alt = (f"anywhere (no VPN, no LAN):\n"
                   f"  curl -fsSL --connect-timeout 5 {pub}/join.sh | sh\n"
                   if pub and pub != base else "")
            return (
                f"join this clique (one line, no token needed):\n"
                f"  curl -fsSL --connect-timeout 5 {base}/join.sh | sh\n"
                f"{alt}"
                f"then one step (buttons, no scripts):\n"
                f"  export CLIQUE_SERVER={base}\n"
                f"  clique              # Host Join Chat Dashboard buttons\n"
                f"or headless (no buttons):\n"
                f"  clique join --server {base} --runtime echo --param-b 7\n"
                f"or without installing anything:\n"
                f"  curl -fsSL {base}/tui.py | python3 - --server "
                f"{base}\n"
                f"browser:   {base}/dash (live) · {base}/chat (ask it)\n"
                f"snapshot:  curl -s {base}/dash.txt\n"
                f"git-local: curl -s {base}/repo.bundle -o /tmp/tcj.bundle "
                f"&& git clone /tmp/tcj.bundle ~/tcj\n"
            )

        @app.get("/dash.txt", response_class=PlainTextResponse)
        async def dash_txt() -> str:
            """Plain-text dashboard snapshot: curl-only, no python needed."""
            import io
            from client.curl_tui import render_lines
            snap = {
                "clique": {
                    "name": self.config.server.clique_name,
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

        @app.get("/app.tgz")
        async def app_tgz():
            """Source bundle of the running server tree (no GitHub needed).

            Served from git-archive of HEAD; falls back to a tar of the
            server checkout when git is unavailable. The joiner unpacks
            this instead of cloning a repo, so there is no PAT, no ssh
            key, and no GitHub round-trip in the join path."""
            import io as _io
            import subprocess as _sp
            from fastapi.responses import Response as _Response
            from pathlib import Path as _Path

            root = _Path(__file__).resolve().parents[1]
            blob: bytes | None = None
            with contextlib.suppress(Exception):
                blob = _sp.check_output(
                    ["git", "-C", str(root), "archive", "HEAD",
                     "--format=tar"], timeout=30)
                import gzip as _gzip
                blob = _gzip.compress(blob)
            if blob is None:
                import tarfile as _tar
                buf = _io.BytesIO()
                with _tar.open(fileobj=buf, mode="w:gz") as t:
                    for p in sorted(root.rglob("*")):
                        rel = p.relative_to(root)
                        if rel.parts and rel.parts[0] in (
                                ".venv", ".git", "__pycache__",
                                "state-repo", "code-repo", "workspaces",
                                "models"):
                            continue
                        t.add(p, arcname=str(rel))
                blob = buf.getvalue()
            return _Response(content=blob, media_type="application/gzip",
                             headers={"Content-Disposition":
                                      'attachment; filename="clique-app.tgz"'})

        @app.get("/repo.bundle")
        async def repo_bundle():
            """Full git bundle of the server tree (history included).

            Lets a teammate's box become a git remote without GitHub:
            ``curl $SERVER/repo.bundle -o /tmp/tcj.bundle &&
            git clone /tmp/tcj.bundle ~/tcj``. Falls back to /app.tgz
            content (snapshot, no history) when git is unavailable."""
            import subprocess as _sp
            from fastapi.responses import Response as _Response
            from pathlib import Path as _Path

            root = _Path(__file__).resolve().parents[1]
            blob: bytes | None = None
            with contextlib.suppress(Exception):
                import subprocess as _sub
                blob = _sp.check_output(
                    ["git", "-C", str(root), "bundle", "create", "-",
                     "--all"], timeout=60, stderr=_sub.DEVNULL)
            if blob is None:
                return await app_tgz()
            return _Response(content=blob, media_type="application/octet-stream",
                             headers={"Content-Disposition":
                                      'attachment; filename="clique-tcj.bundle"'})

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
            """Self-contained installer: server URL + app bundle baked in.

            No GitHub, no PAT, no ssh key. The joiner only runs:
              curl -fsSL {base}/join.sh | sh
            Source comes from {base}/app.tgz (the running server tree).
            Auth to the clique happens after install via signed
            registration (node keypair -> bearer token), never in shell."""
            from pathlib import Path

            base = str(request.base_url).rstrip("/")
            return _JOIN_SH_TEMPLATE.replace("__CLIQUE_SERVER__", base)

        register_extended_routes(app, self)
        register_ws_routes(app, self)
        register_workspace_routes(app, self)
        return app

    # ------------------------------------------------- workspace realtime

    async def apply_workspace_patch(self, workspace_id: str, path: str,
                                    base_version: int, ops: list[dict],
                                    actor: str) -> dict:
        """Sequenced patch entry: memory apply, broadcast delta, notify agents."""
        event = await self.live_workspaces.apply_patch(
            workspace_id, path, base_version, ops, actor)
        topic = f"workspace:{workspace_id}"
        await self.events.publish(
            "workspace.delta",
            protocol.msg_ws_delta(
                workspace_id, path, event["version"], event["seq"],
                event["ops"], actor, event["rebased"]),
            topic=topic)
        await self.events.publish("workspace.patched", {  # firehose
            "workspace_id": workspace_id, "path": path,
            "seq": event["seq"], "actor": actor})
        # notify running agents: push invalidate over /ws/agent so their
        # next inference chunk rereads instead of using stale context
        invalidate = protocol.dumps(protocol.msg_ws_invalidate(
            workspace_id, event["seq"], [path]))
        for node_id in self.conns.connected_ids():
            ws = self.conns.get(node_id)
            if ws is not None:
                try:
                    await ws.send_text(invalidate)
                except Exception:
                    pass  # tick loop cleans up dead conns
        return event

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
            await self.events.publish(
                "task.progress",
                {"task_id": tid, "delta": msg["text_delta"]},
                topic=f"task:{tid}")
            view = self.router.get_task(tid)
            if view and view.request.relays_session_stream():
                await self.events.publish(
                    "session.delta",
                    {"task_id": tid, "delta": msg["text_delta"]},
                    topic=f"session:{view.request.session_id}")
        elif mtype == protocol.RESULT:
            result = TaskResult.model_validate(msg["result"])
            result = await self._verify_code_result(result)
            try:
                committed = self.router.on_result(result)
            except Exception as e:  # stale attempt etc.
                log.warning("result rejected: %s", e)
                committed = False
            if committed:
                self.progress.pop(result.task_id, None)
                view = self.router.get_task(result.task_id)
                if view is not None and view.state.value in (
                        "succeeded", "failed", "cancelled", "expired"):
                    self.ledger.record(view)
                    await self._settle_race(view)
                # agent turn end -> checkpoint its workspace so the result
                # is rollbackable even before the debounce fires
                wid = view.request.workspace_id if view else None
                if wid:
                    with contextlib.suppress(Exception):
                        await self.live_workspaces.force_flush(wid)
                req = view.request if view else None
                sid = req.session_id if req else None
                if (sid and result.state == TaskState.SUCCEEDED
                        and result.output and req is not None):
                    if (not req.record_turns
                            and req.task_type == TaskType.SUMMARIZATION):
                        try:
                            self._commit_compaction(
                                sid, result.output, req.compaction_covers or [])
                        except SessionConflictError:
                            log.warning(
                                "compaction turn lost to version conflict "
                                "on session %s", sid)
                        else:
                            await self.events.publish(
                                "session.compacted",
                                {"session_id": sid, "task_id": result.task_id},
                                topic=f"session:{sid}")
                        self._compacting.discard(sid)
                    elif req.record_turns:
                        try:
                            self.sessions.append_turn_latest(
                                sid, "assistant", result.output)
                        except SessionConflictError:
                            log.warning(
                                "assistant turn lost to version conflict "
                                "on session %s", sid)
                        else:
                            self.sessions.pin(sid, node_id)
                            await self.events.publish(
                                "session.turn", {"session_id": sid,
                                                 "task_id": result.task_id},
                                topic=f"session:{sid}")
                            await self._maybe_compact(sid, req.max_output_tokens)
                elif (sid and req is not None and not req.record_turns
                      and result.state != TaskState.SUCCEEDED):
                    self._compacting.discard(sid)
                await self.events.publish("task.finished", {
                    "task_id": result.task_id, "state": result.state.value,
                    "node_id": node_id})
                await self.events.publish("task.finished", {
                    "task_id": result.task_id, "state": result.state.value,
                    "node_id": node_id}, topic=f"task:{result.task_id}")
            self.registry.set_status(node_id, NodeStatus.READY, current_task_id=None)
            await self._schedule_now()
        elif mtype == protocol.LEAVE:
            info = self.registry.get(node_id)
            task_age = (self.router.task_age_s(info.current_task_id)
                       if info and info.current_task_id else 0.0)
            self.registry.remove(node_id, task_age)
            for tid in self.router.on_node_lost(node_id):
                log.info("requeued %s after %s left", tid, node_id)
            await self.events.publish("node.left", {"node_id": node_id})
            await self._schedule_now()

    # -------------------------------------------------------------- shutdown

    async def _revoke_assignment(self, task_id: str, attempt_id: str | None,
                                 node_id: str, reason: str) -> None:
        ws = self.conns.get(node_id)
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send_text(protocol.dumps(protocol.msg_revoke(
                    task_id, attempt_id or "", reason)))
        self.registry.set_status(
            node_id, NodeStatus.READY, current_task_id=None)

    async def delete_session(self, session_id: str, actor: str = "server") -> bool:
        try:
            self.sessions.delete(session_id)
        except KeyError:
            return False
        self._compacting.discard(session_id)
        await self.events.publish(
            "session.deleted", {"session_id": session_id, "actor": actor})
        return True

    async def clear_sessions(self, actor: str = "server") -> dict:
        n = self.sessions.clear()
        turns = self.contexts.clear()
        self._compacting.clear()
        counts = {"sessions": n, "context_turns": turns}
        await self.events.publish("sessions.cleared", {"actor": actor, **counts})
        return counts

    async def delete_task(self, task_id: str, actor: str = "server") -> bool:
        view = self.router.get_task(task_id)
        if view is None:
            return False
        if (view.state.value in ("assigned", "running")
                and view.assigned_node):
            await self._revoke_assignment(
                task_id, view.attempt_id, view.assigned_node, "task deleted")
        self.workspaces.cleanup(task_id)
        self.progress.pop(task_id, None)
        for race_id, members in list(self.race_groups.items()):
            kept = [t for t in members if t != task_id]
            if kept:
                self.race_groups[race_id] = kept
            else:
                del self.race_groups[race_id]
        self.router.delete(task_id)
        await self.events.publish(
            "task.deleted", {"task_id": task_id, "actor": actor})
        return True

    async def clear_queue(self, actor: str = "server") -> dict:
        revoked = 0
        for task_id, attempt_id, node_id in self.router.inflight_assignments():
            await self._revoke_assignment(
                task_id, attempt_id, node_id, "queue cleared")
            self.workspaces.cleanup(task_id)
            revoked += 1
        n = self.router.clear()
        ws = self.workspaces.clear()
        self.progress.clear()
        self.race_groups.clear()
        counts = {"tasks": n, "revoked": revoked, "workspaces": ws}
        await self.events.publish("queue.cleared", {"actor": actor, **counts})
        return counts

    async def clear_data(self, actor: str = "server") -> dict:
        """Wipe sessions, the task queue, and stored operational data.

        Nodes stay registered and connected. In-flight tasks are revoked
        so agents stop work that no longer has a queue row. Git history
        in state-repo / code-repo is left alone (audit trail).
        """
        revoked = 0
        for task_id, attempt_id, node_id in self.router.inflight_assignments():
            await self._revoke_assignment(
                task_id, attempt_id, node_id, "server data cleared")
            revoked += 1

        counts = {
            "sessions": self.sessions.clear(),
            "context_turns": self.contexts.clear(),
            "tasks": self.router.clear(),
            "ledger": self.ledger.clear(),
            "workspaces": self.workspaces.clear(),
            "live_workspaces": self.live_workspaces.clear(),
            "suggestions": self.suggestions.clear(),
            "revoked": revoked,
        }
        self.progress.clear()
        self.race_groups.clear()
        self._compacting.clear()
        await self.events.publish("server.cleared", {"actor": actor, **counts})
        log.info("cleared server data: %s", counts)
        return counts

    async def shutdown_active_tasks(self) -> list[dict]:
        """Nodes currently holding a task, for the confirm-before-kill
        check on POST /v1/server/shutdown."""
        return [{"node_id": n.node_id, "display_name": n.display_name,
                "task_id": n.current_task_id}
               for n in self.registry.list_nodes() if n.current_task_id]

    async def shutdown_now(self, reason: str = "server shutdown") -> None:
        """Tell every connected agent to disconnect, then trigger this
        process's own graceful exit (uvicorn already shuts down cleanly on
        SIGTERM; reusing that path instead of a second exit mechanism)."""
        import os
        import signal
        for node_id, ws in self.conns.items():
            with contextlib.suppress(Exception):
                await ws.send_text(protocol.dumps(protocol.msg_shutdown(reason)))
        await asyncio.sleep(0.5)  # let SHUTDOWN frames flush before we go down
        os.kill(os.getpid(), signal.SIGTERM)

    # ------------------------------------------------------- code-edit verify

    async def _verify_code_result(self, result: TaskResult) -> TaskResult:
        """Server-side harness gate for CODE_EDIT tasks.

        Extracts the fenced diff from raw node output, applies it in an
        ephemeral workspace seeded from the task spec, runs the allowlisted
        test command, and on success records patch/test_report/applied_sha.
        On any failure the result is rewritten to FAILED with a
        machine-readable error prefix so router retry still applies.
        Non-code tasks pass through untouched.
        """
        from common.types import TaskType as _TaskType
        view = self.router.get_task(result.task_id)
        if view is None or view.request.task_type != _TaskType.CODE_EDIT:
            return result
        if view.request.code is None or result.state != TaskState.SUCCEEDED:
            return result
        spec = view.request.code
        node_id = view.assigned_node or "server"

        def fail(prefix: str) -> TaskResult:
            self.workspaces.cleanup(result.task_id)
            return TaskResult(
                task_id=result.task_id, attempt_id=result.attempt_id,
                state=TaskState.FAILED, output=result.output,
                error=prefix, prompt_tokens=result.prompt_tokens,
                output_tokens=result.output_tokens,
                wall_time_s=result.wall_time_s)

        from scheduler import harness as _h
        try:
            diff = _h.extract_diff(result.output or "")
            _h.validate_diff(diff, spec)
        except Exception as e:
            return fail(str(e))
        try:
            self.workspaces.create(result.task_id, spec)
            self.workspaces.apply_patch(result.task_id, diff)
            report = self.workspaces.run_tests(result.task_id, spec)
        except Exception as e:
            failed = fail(str(e))
            # keep the test report on failure so /tests shows why
            rep = getattr(e, "report", None)
            if rep is not None:
                failed.test_report = rep
            return failed
        # Publish verified files into code-repo working tree, then commit.
        # Collect every file in the verified workspace (seed + patch adds).
        # Skip test-run artifacts (__pycache__, .pytest_cache, .pyc).
        ws_root = self.workspaces.path_for(result.task_id)
        rels: list[str] = []
        for p in sorted(ws_root.rglob("*")):
            if not p.is_file() or p.name == ".harness.patch":
                continue
            rel = str(p.relative_to(ws_root))
            parts = rel.split("/")
            if parts[0] in ("__pycache__", ".pytest_cache", ".hypothesis"):
                continue
            if p.suffix in (".pyc", ".pyo"):
                continue
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue  # binary artifact, not source
            dst = self.code_vcs.repo_dir / result.task_id / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text)
            rels.append(f"{result.task_id}/{rel}")
        sha = self.code_vcs.commit_paths(
            rels, f"code task {result.task_id} accepted", node_id)
        if sha is None:
            return fail("commit_empty: no changes staged")
        self.workspaces.cleanup(result.task_id)
        result.patch = diff
        result.test_report = report
        result.applied_sha = sha
        return result

    # ------------------------------------------------------- race + upkeep

    async def _settle_race(self, view) -> None:
        """Cancel siblings when a race member is accepted, with live revoke.

        Race members share an idempotency_key prefix ``race:<id>``.
        First harness-accepted patch wins; siblings are cancelled, their
        workspaces cleaned, and live nodes get a REVOKE over WS so they
        stop burning inference immediately.
        """
        key = view.request.idempotency_key
        if not key.startswith("race:"):
            return
        race_id = key.split(":", 2)[1] if ":" in key else ""
        members = self.race_groups.get(race_id, [])
        if not members:
            return
        accepted = (view.state.value == "succeeded" and view.result
                    and view.result.applied_sha)
        if not accepted:
            return
        for tid in members:
            if tid == view.request.task_id:
                continue
            sib = self.router.get_task(tid)
            if sib is None:
                continue
            if self.router.cancel(tid):
                self.workspaces.cleanup(tid)
                self.progress.pop(tid, None)
                # unblock live watchers (CLI streams both race members;
                # without this the loser's WS hangs to timeout)
                await self.events.publish("task.finished", {
                    "task_id": tid, "state": "cancelled",
                    "node_id": sib.assigned_node}, topic=f"task:{tid}")
                # live revoke: tell the node to cancel now, not on lease
                if sib.assigned_node and sib.attempt_id:
                    ws = self.conns.get(sib.assigned_node)
                    if ws is not None:
                        with contextlib.suppress(Exception):
                            await ws.send_text(protocol.dumps(
                                protocol.msg_revoke(
                                    tid, sib.attempt_id,
                                    f"race {race_id} won by "
                                    f"{view.request.task_id}")))
                    self.registry.set_status(
                        sib.assigned_node, NodeStatus.READY,
                        current_task_id=None)
        del self.race_groups[race_id]

    def gc_workspaces(self, older_than_s: float = 3600.0) -> int:
        """Remove orphaned workspaces (no live task). Returns count."""
        import time as _time
        live = {v.request.task_id for v in self.router.list_tasks()}
        removed = 0
        for child in self.workspaces.base_dir.iterdir():
            if not child.is_dir() or child.name in live:
                continue
            try:
                age = _time.time() - child.stat().st_mtime
            except OSError:
                continue
            if age > older_than_s:
                self.workspaces.cleanup(child.name)
                removed += 1
        # Drop race groups whose members all reached terminal state.
        dead_races = []
        for race_id, members in self.race_groups.items():
            states = []
            for tid in members:
                v = self.router.get_task(tid)
                states.append(v.state.value if v else "missing")
            if all(s in ("succeeded", "failed", "cancelled", "expired",
                         "missing") for s in states):
                # keep winners visible briefly: only GC when no live member
                live_member = any(
                    (self.router.get_task(t) is not None and
                     self.router.get_task(t).state.value in
                     ("queued", "assigned", "running")) for t in members)
                if not live_member:
                    dead_races.append(race_id)
        for race_id in dead_races:
            del self.race_groups[race_id]
        return removed

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
            if request.session_id:
                try:
                    self.sessions.pin(request.session_id, assignment.node_id)
                    node = self.registry.get(assignment.node_id)
                    if node and node.model:
                        self.sessions.bind_cluster(
                            request.session_id, node.model.cluster_key())
                except KeyError:
                    pass
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

    async def _reap_offline(self) -> None:
        """Permanently drop roster entries that have been offline past
        their grace period: the age of the task they were last running
        (capped), so a brief network blip mid a long task gets time to
        reconnect while an idle node is dropped right away."""
        cfg = self.config.server
        now = utcnow()
        for info in self.registry.list_nodes(status=NodeStatus.OFFLINE):
            if info.role == NodeRole.SERVER or info.offline_since is None:
                continue
            grace = min(info.last_task_duration_s, cfg.reap_grace_max_s)
            if (now - info.offline_since).total_seconds() > grace:
                log.info("reaping node %s (offline %.0fs, grace %.0fs)",
                         info.node_id, (now - info.offline_since).total_seconds(), grace)
                self.registry.delete(info.node_id)
                await self.events.publish("node.reaped", {"node_id": info.node_id})

    async def _tick_loop(self) -> None:
        hb = self.config.node.heartbeat_interval_s
        offline_after = timedelta(seconds=hb * self.config.server.heartbeat_offline_after)
        snapshot_every = 30.0  # seconds between periodic vcs snapshots
        last_snapshot = 0.0
        import time as _time
        while True:
            await asyncio.sleep(hb)
            try:
                for info in self.registry.mark_offline_stale(
                        offline_after, task_age_fn=self.router.task_age_s):
                    log.info("node %s offline (stale heartbeat)", info.node_id)
                    for tid in self.router.on_node_lost(info.node_id):
                        log.info("requeued %s", tid)
                    await self.events.publish(
                        "node.offline", {"node_id": info.node_id})
                await self._reap_offline()
                self.router.expire_leases()
                for s in self.suggestions.analyze():
                    await self.events.publish("suggestion.new", s.to_dict())
                await self.events.publish(
                    "stats.tick", self.router.queue_stats())
                now = _time.monotonic()
                if now - last_snapshot > snapshot_every:
                    last_snapshot = now
                    self.vcs.snapshot("periodic state snapshot", "server")
                    self.gc_workspaces()
                    # durable safety net: checkpoint dirty live workspaces
                    # even if their debounce tasks were lost on restart
                    for wid in self.live_workspaces.list_ids():
                        try:
                            ws = self.live_workspaces.get(wid)
                        except KeyError:
                            continue
                        if ws.dirty:
                            with contextlib.suppress(Exception):
                                await self.live_workspaces.force_flush(wid)
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
