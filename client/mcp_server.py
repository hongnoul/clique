"""MCP server exposing clique inference to any MCP client (stdio).

Thin wrapper over :class:`client.sdk.CliqueClient`: auth, 401 re-auth and
mDNS discovery come for free. Tools favor the async task surface (submit /
status / wait) because MCP callers usually offload delay-tolerant work;
``clique_chat`` is the blocking convenience path.

Server resolution order:
  1. ``CLIQUE_URL`` env var (e.g. ``http://127.0.0.1:8000``)
  2. zeroconf discovery on the LAN

Run: ``clique-mcp`` (stdio transport), or register with any MCP host:
  claude mcp add clique -- clique-mcp
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from client.sdk import CliqueClient
from common.types import TaskState, TaskView

mcp = MCPServer(
    "clique",
    instructions=(
        "Inference on the clique: a LAN pool of local model nodes. "
        "Use clique_chat for a quick blocking completion. For long or "
        "batch work, prefer clique_submit then clique_wait or "
        "clique_task_status so you can do other things in between. "
        "Code tasks (clique_code_task) run against provided files and "
        "are verified with tests on the worker before commit. "
        "Live workspaces (clique_workspace_*) are the clique's realtime "
        "socket VCS: shared files with sequenced writes, rebase on "
        "conflict, and git checkpoints. Use them to share context with "
        "other agents and humans working the same files: read before "
        "editing, write through the workspace so everyone sees your "
        "change, and pass workspace_id to code tasks."
    ),
)

_client: CliqueClient | None = None

_TERMINAL = (TaskState.SUCCEEDED, TaskState.FAILED,
             TaskState.CANCELLED, TaskState.EXPIRED)


async def _get_client() -> CliqueClient:
    """Lazy singleton: env URL first, LAN discovery as fallback."""
    global _client
    if _client is None:
        url = os.environ.get("CLIQUE_URL", "").strip()
        if url:
            c = CliqueClient(url)
        else:
            c = await CliqueClient.discover()
        await c.authenticate()
        _client = c
    return _client


def _view_dict(view: TaskView) -> dict[str, Any]:
    """Terminal TaskView -> compact tool payload."""
    out: dict[str, Any] = {
        "task_id": view.request.task_id,
        "state": view.state.value,
        "output": (view.result.output or "") if view.result else "",
    }
    if view.result:
        if view.result.error:
            out["error"] = view.result.error
        if view.result.output_tokens:
            out["output_tokens"] = view.result.output_tokens
    if view.assigned_node:
        out["node"] = view.assigned_node
    return out


@mcp.tool()
async def clique_chat(prompt: str, model: str | None = None,
                      session_id: str | None = None,
                      max_output_tokens: int = 1024,
                      timeout_s: float = 300.0) -> dict:
    """Run one blocking completion on the clique and return the output.

    Pass session_id (from clique_session) to keep multi-turn context.
    On timeout the task keeps running; the partial output and task_id
    are returned so you can resume with clique_wait.
    """
    c = await _get_client()
    task_id = await c.submit(prompt, model_hint=model, session_id=session_id,
                             max_output_tokens=max_output_tokens)
    try:
        view = await c.wait(task_id, timeout_s=timeout_s)
    except TimeoutError:
        data = await c.task(task_id)
        return {"task_id": task_id, "state": "running", "timed_out": True,
                "partial_output": data.get("partial_output", "") or ""}
    return _view_dict(view)


@mcp.tool()
async def clique_submit(prompt: str, model: str | None = None,
                        session_id: str | None = None,
                        max_output_tokens: int = 1024) -> dict:
    """Submit an async inference task; returns task_id immediately.

    Poll with clique_task_status or block with clique_wait.
    """
    c = await _get_client()
    task_id = await c.submit(prompt, model_hint=model, session_id=session_id,
                             max_output_tokens=max_output_tokens)
    return {"task_id": task_id}


@mcp.tool()
async def clique_task_status(task_id: str) -> dict:
    """Get task state plus partial or final output. Non-blocking."""
    c = await _get_client()
    data = await c.task(task_id)
    view = TaskView.model_validate(data)
    if view.state in _TERMINAL:
        return _view_dict(view)
    return {"task_id": task_id, "state": view.state.value,
            "partial_output": data.get("partial_output", "") or ""}


@mcp.tool()
async def clique_wait(task_id: str, timeout_s: float = 300.0) -> dict:
    """Block until a task finishes (or timeout_s) and return its output."""
    c = await _get_client()
    try:
        view = await c.wait(task_id, timeout_s=timeout_s)
    except TimeoutError:
        data = await c.task(task_id)
        return {"task_id": task_id, "state": data.get("state", "running"),
                "timed_out": True,
                "partial_output": data.get("partial_output", "") or ""}
    return _view_dict(view)


@mcp.tool()
async def clique_cancel(task_id: str) -> dict:
    """Cancel a running task."""
    c = await _get_client()
    return {"task_id": task_id, "cancelled": await c.cancel(task_id)}


@mcp.tool()
async def clique_code_task(prompt: str, files: dict[str, str],
                           test_cmd: list[str] | None = None,
                           timeout_s: float = 600.0) -> dict:
    """Run a verified code-edit task: the worker edits the given files
    (path -> content) and the diff only commits if test_cmd passes
    (default: pytest -q). Returns the diff and test report.
    """
    c = await _get_client()
    task_id = await c.code_submit(prompt, files, test_cmd=test_cmd)
    try:
        view = await c.wait(task_id, timeout_s=timeout_s)
    except TimeoutError:
        return {"task_id": task_id, "state": "running", "timed_out": True}
    result = _view_dict(view)
    try:
        result["diff"] = await c.code_diff(task_id)
    except Exception:
        pass
    try:
        result["tests"] = await c.code_tests(task_id)
    except Exception:
        pass
    return result


@mcp.tool()
async def clique_session(cluster_key: str = "") -> dict:
    """Create a chat session for multi-turn context. Pass the returned
    session_id to clique_chat / clique_submit on later turns.
    """
    c = await _get_client()
    return await c.create_session(cluster_key)


@mcp.tool()
async def clique_status() -> dict:
    """Cluster health: nodes, models, and queue stats."""
    c = await _get_client()
    nodes = await c.nodes()
    stats = await c.stats()
    return {
        "nodes": [{
            "node_id": n.node_id, "name": n.display_name,
            "status": n.status.value,
            "model": n.model.family if n.model else None,
            "param_b": n.model.parameter_count_b if n.model else None,
        } for n in nodes],
        "stats": stats,
    }


@mcp.tool()
async def clique_models() -> list[str]:
    """List routable model names (pass one as `model` to clique_chat)."""
    c = await _get_client()
    clusters = await c.clusters()
    seen: list[str] = ["clique"]
    for cl in clusters:
        for cand in (cl.cluster_key, cl.model.family):
            if cand and cand not in seen:
                seen.append(cand)
    return seen


# ---------------------------------------------------------------------------
# Live workspaces: the clique's realtime socket VCS (shared agent context)
# ---------------------------------------------------------------------------

@mcp.tool()
async def clique_workspace_create(files: dict[str, str] | None = None) -> dict:
    """Create a shared live workspace (realtime socket VCS).

    `files` maps path -> initial content. Returns workspace_id + seq.
    Every write by any agent is sequenced, broadcast live to WS
    subscribers, and checkpointed to git on the server.
    """
    c = await _get_client()
    return await c.workspace_create(files or {})


@mcp.tool()
async def clique_workspace_list() -> list[dict]:
    """List live workspaces with their head seq."""
    c = await _get_client()
    return await c.workspaces()


@mcp.tool()
async def clique_workspace_read(workspace_id: str,
                                path: str | None = None) -> dict:
    """Read a workspace: full snapshot, or one file (with text + version)
    when `path` is given. Always read before writing so your edit is
    based on the latest shared state.
    """
    c = await _get_client()
    if path:
        return await c.workspace_file(workspace_id, path)
    return await c.workspace(workspace_id)


@mcp.tool()
async def clique_workspace_write(workspace_id: str, path: str,
                                 text: str) -> dict:
    """Replace one file's content in the shared workspace. The write is
    sequenced against the current head (stale bases are rebased, never
    rejected) and broadcast to every live subscriber and running agent.
    """
    c = await _get_client()
    return await c.workspace_write(workspace_id, path, text)


@mcp.tool()
async def clique_workspace_patch(workspace_id: str, path: str,
                                 ops: list[dict],
                                 base_version: int = 0) -> dict:
    """Line-level patch of a workspace file. Ops:
    {"op":"insert","line":N,"text":...}, {"op":"delete","line":N,"count":M},
    {"op":"replace_file","text":...}. Lines are 1-indexed; pass the
    version you read as base_version so concurrent edits rebase cleanly.
    """
    c = await _get_client()
    return await c.workspace_patch(workspace_id, path, ops,
                                   base_version=base_version)


@mcp.tool()
async def clique_workspace_history(workspace_id: str,
                                   path: str | None = None,
                                   limit: int = 50) -> list[dict]:
    """Recent op log: who changed what, in global seq order, with
    rebase flags. Use this to see other agents' live activity.
    """
    c = await _get_client()
    return await c.workspace_history(workspace_id, path=path, limit=limit)


@mcp.tool()
async def clique_workspace_flush(workspace_id: str) -> dict:
    """Force a git checkpoint of the workspace now; returns the sha."""
    c = await _get_client()
    return await c.workspace_flush(workspace_id)


@mcp.tool()
async def clique_workspace_task(prompt: str, workspace_id: str,
                                paths: list[str] | None = None,
                                test_cmd: list[str] | None = None,
                                timeout_s: float = 600.0) -> dict:
    """Run a verified code-edit task against live workspace files.

    The worker sees the current shared content at submit time and gets
    invalidation pushes if files move mid-task. On success the diff is
    committed and the workspace is flushed, so other agents see the
    result immediately.
    """
    c = await _get_client()
    snap = await c.workspace(workspace_id)
    files: dict[str, str] = {}
    for p, f in snap.get("files", {}).items():
        if paths and p not in paths:
            continue
        text = f.get("text")
        if text is None:
            text = (await c.workspace_file(workspace_id, p))["text"]
        files[p] = text
    task_id = await c.code_submit(prompt, files, test_cmd=test_cmd,
                                  workspace_id=workspace_id)
    try:
        view = await c.wait(task_id, timeout_s=timeout_s)
    except TimeoutError:
        return {"task_id": task_id, "state": "running", "timed_out": True}
    result = _view_dict(view)
    try:
        result["diff"] = await c.code_diff(task_id)
    except Exception:
        pass
    return result


def main() -> None:
    """Entry point: serve MCP over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
