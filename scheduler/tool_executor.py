"""Server-side tool executor: run worker-requested tool calls.

The worker (node agent) cannot reach MCP servers or the database; it
only has the task prompt. When the model emits OpenAI-style tool_calls,
the agent relays each one over the agent WS (TOOL_CALL) and the server
executes it here against its own live state, then replies TOOL_RESULT.

Scope guard: only read/write operations on the server's own surfaces
are exposed (workspaces, sessions, vcs history, stats, ledger). No
shell, no network fetch, no admin ops (kick, rollback, shutdown).
Unknown tools fail closed with an error string, never an exception.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from scheduler.server import SchedulerServer

log = logging.getLogger("clique.tools")

#: Max serialized chars per tool result (protects the model context).
RESULT_LIMIT = 8000


def _truncate(obj: Any, limit: int = RESULT_LIMIT) -> Any:
    """Cap serialized size, preserving structure for small results.

    Dicts/lists pass through untouched when under the limit; only
    oversized payloads collapse to a truncated JSON string.
    """
    try:
        size = len(obj) if isinstance(obj, str) else len(json.dumps(obj))
    except (TypeError, ValueError):
        return str(obj)[:limit]
    if size <= limit:
        return obj
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return text[:limit] + f"... [truncated {size - limit} chars]"


async def execute_tool(server: "SchedulerServer", task_id: str,
                       name: str, arguments: dict) -> tuple[bool, Any]:
    """Execute one tool call. Returns (ok, result-or-error-string)."""
    args = arguments if isinstance(arguments, dict) else {}
    try:
        if name == "clique_self_assess":
            return True, _truncate(await _self_assess(server))
        if name == "clique_workspace_create":
            ws = server.live_workspaces.create(
                workspace_id=args.get("workspace_id"),
                initial_files=args.get("files") or {})
            return True, {"workspace_id": ws.workspace_id, "seq": ws.seq}
        if name == "clique_workspace_list":
            return True, [{"workspace_id": wid,
                           "seq": server.live_workspaces.get(wid).seq}
                          for wid in server.live_workspaces.list_ids()]
        if name == "clique_workspace_read":
            wid = args.get("workspace_id", "")
            path = args.get("path")
            if path:
                return True, _truncate(
                    server.live_workspaces.file_state(wid, path))
            return True, _truncate(server.live_workspaces.snapshot(wid))
        if name in ("clique_workspace_write", "clique_workspace_patch"):
            wid = args.get("workspace_id", "")
            path = args.get("path", "")
            actor = f"task:{task_id}"
            if name == "clique_workspace_write":
                ops = [{"op": "replace_file", "text": args.get("text", "")}]
                try:
                    st = server.live_workspaces.file_state(wid, path)
                    base = int(st.get("version", 0))
                except KeyError:
                    base = 0
            else:
                ops = args.get("ops", [])
                base = int(args.get("base_version", 0))
            event = await server.apply_workspace_patch(
                wid, path, base, ops, actor)
            return True, event
        if name == "clique_workspace_history":
            return True, _truncate(server.live_workspaces.history(
                args.get("workspace_id", ""), args.get("path"),
                int(args.get("limit", 50) or 50)))
        if name == "clique_workspace_flush":
            sha = await server.live_workspaces.force_flush(
                args.get("workspace_id", ""))
            return True, {"sha": sha}
        if name == "clique_workspace_export":
            wid = args.get("workspace_id", "")
            try:
                snap = server.live_workspaces.snapshot(wid)
            except KeyError:
                return False, f"no such workspace: {wid}"
            wanted = args.get("paths")
            files = {p: f["text"] for p, f in snap["files"].items()
                     if not wanted or p in wanted}
            try:
                commits = server.live_workspaces.git_history(wid, 10)
            except Exception:
                commits = []
            return True, _truncate({"workspace_id": wid,
                                    "seq": snap["seq"], "files": files,
                                    "commits": commits})
        if name == "clique_status":
            nodes = [{"node_id": n.node_id, "name": n.display_name,
                      "status": n.status.value,
                      "model": n.model.family if n.model else None}
                     for n in server.registry.list_nodes()]
            stats = server.router.queue_stats()
            return True, _truncate({"nodes": nodes, "queue": stats})
        if name == "clique_models":
            seen = ["clique"]
            try:
                clusters = server.registry.list_clusters()
            except Exception:
                clusters = []
            for cl in clusters:
                for cand in (cl.cluster_key,
                             cl.model.family if cl.model else None):
                    if cand and cand not in seen:
                        seen.append(cand)
            return True, seen
        if name == "clique_session":
            session = server.sessions.create(
                owner_node=f"task:{task_id}",
                cluster_key=args.get("cluster_key", ""))
            return True, session.model_dump(mode="json")
        return False, f"unknown tool: {name}"
    except KeyError as e:
        return False, f"no such workspace: {e}"
    except ValueError as e:
        return False, str(e)
    except Exception as e:  # never leak tracebacks to the model
        log.warning("tool %s failed: %s", name, e)
        return False, f"tool error: {e}"


async def _self_assess(server: "SchedulerServer") -> dict:
    """Same aggregation as the MCP clique_self_assess tool."""
    from scheduler.server import _public_url, _server_sha
    nodes = server.registry.list_nodes()
    stats = server.router.queue_stats()
    ledger = stats.get("ledger", {}) if isinstance(stats, dict) else {}
    try:
        raw = server.suggestions.analyze()
        suggestions = [s if isinstance(s, dict) else {
            "suggestion_id": getattr(s, "suggestion_id", ""),
            "kind": getattr(s, "kind", ""),
            "rationale": getattr(s, "rationale", "")} for s in raw]
    except Exception:
        suggestions = []
    try:
        vcs = server.vcs.history(limit=5)
    except Exception:
        vcs = []
    try:
        workspaces = server.live_workspaces.list_ids()
    except Exception:
        workspaces = []
    ready = [n for n in nodes if n.status.value == "ready"]
    busy = [n for n in nodes if n.status.value == "busy"]
    task_states = stats.get("by_state", {}) or {}
    queued = int(task_states.get("queued", 0) or 0)
    lstates = ledger.get("by_state", {}) if isinstance(ledger, dict) else {}
    succeeded = int(lstates.get("succeeded", 0) or 0)
    failed = int(lstates.get("failed", 0) or 0)
    total = succeeded + failed
    findings: list[str] = []
    if not ready and not busy:
        findings.append("CRITICAL: no ready nodes.")
    if queued > 0 and not ready:
        findings.append(f"{queued} queued with no free capacity.")
    elif queued >= 5:
        findings.append(f"queue backlog: {queued} waiting.")
    if total >= 10 and failed / total > 0.2:
        findings.append(f"high failure rate: {failed}/{total}.")
    for s in suggestions:
        s = s if isinstance(s, dict) else s.__dict__
        findings.append(f"suggestion[{s.get('kind')}]: "
                        f"{s.get('rationale', '')}")
    if not findings:
        findings.append("healthy: capacity available, no backlog, "
                        "no pending suggestions.")
    return {
        "server": {"sha": _server_sha(),
                   "public_url": _public_url()},
        "fleet": {"ready": len(ready), "busy": len(busy),
                  "total": len(nodes)},
        "queue": {k: v for k, v in stats.items()
                  if k not in ("nodes", "ledger")},
        "ledger": ledger,
        "suggestions": suggestions,
        "recent_state_changes": vcs,
        "live_workspaces": len(workspaces),
        "findings": findings,
    }
