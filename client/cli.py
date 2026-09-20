"""Headless CLI (MVP implementation).

Commands:
  clique serve                       run the server node here
  clique join [--server URL] ...     run an agent on this device
  clique nodes [--server URL]        list nodes and clusters
  clique dash [--server URL] [--full]  live terminal dashboard (polls /dash.txt)
  clique submit PROMPT               chat turn (sticky session by default)
  clique task TASK_ID [--cancel]     inspect or cancel a task
  clique stats                       queue and node stats
"""

from __future__ import annotations

import asyncio

import httpx
import typer
from rich.console import Console
from rich.table import Table

from client.sdk import CliqueClient

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _resolve(server: str | None) -> CliqueClient:
    if server:
        url = server if server.startswith("http") else f"http://{server}"
        return CliqueClient(url)
    async def _d() -> CliqueClient:
        return await CliqueClient.discover()
    return asyncio.run(_d())


@app.command()
def serve() -> None:
    """Run the clique server node on this device."""
    from scheduler.server import main as server_main
    import sys
    sys.argv = ["clique-server"]
    server_main()


@app.command()
def join(server: str = typer.Option(None, help="server URL, skips mDNS"),
         name: str = typer.Option(None, help="display name, defaults to hostname"),
         runtime: str = typer.Option(None, help="echo | openai-compat"),
         model_name: str = typer.Option(None, help="e.g. qwen2.5-coder:7b"),
         param_b: float = typer.Option(None, help="model size in B params")) -> None:
    """Join this device to the clique as a node."""
    from node.agent import main as agent_main
    import sys
    argv = ["clique-agent"]
    if server:
        argv += ["--server", server]
    if name:
        argv += ["--name", name]
    if runtime:
        argv += ["--runtime", runtime]
    if model_name:
        argv += ["--model-name", model_name]
    if param_b:
        argv += ["--param-b", str(param_b)]
    sys.argv = argv
    agent_main()


@app.command()
def nodes(server: str = typer.Option(None)) -> None:
    """List nodes and clusters."""
    client = _resolve(server)

    async def run() -> None:
        node_list = await client.nodes()
        table = Table(title="clique nodes")
        for col in ("name", "id", "role", "op", "status", "model", "task"):
            table.add_column(col)
        for n in node_list:
            table.add_row(
                n.display_name, n.node_id[:8], n.role.value, n.op_level.value,
                n.status.value,
                n.model.cluster_key() if n.model else "-",
                n.current_task_id or "-")
        console.print(table)
        for c in await client.clusters():
            console.print(f"cluster [bold]{c.cluster_key}[/]: {len(c.node_ids)} node(s)")

    asyncio.run(run())


@app.command()
def submit(prompt: str,
           server: str = typer.Option(None),
           model: str = typer.Option(
               None, "--model",
               help="cluster key prefix (hard filter). omit for auto routing"),
           task_type: str = typer.Option(
               "chat", "--type",
               help="chat | code_generation | summarization | embedding | other"),
           session: str = typer.Option(
               None, "--session", help="session id (default: sticky CLI session)"),
           new_session: bool = typer.Option(
               False, "--new-session", help="start a fresh sticky session"),
           no_session: bool = typer.Option(
               False, "--no-session", help="one-shot task, no conversation memory"),
           max_tokens: int = typer.Option(1024)) -> None:
    """Submit a chat turn and wait for the result.

    Consecutive submits reuse a sticky session so the model remembers
    earlier turns. Pass --no-session for a stateless one-shot.
    """
    client = _resolve(server)

    async def run() -> None:
        sid = session
        if no_session:
            sid = None
        elif not sid:
            await client.authenticate()
            sid = await client.ensure_chat_session(
                model or "", reset=new_session)
        try:
            task_id = await client.submit(prompt, model_hint=model,
                                          task_type=task_type,
                                          session_id=sid,
                                          max_output_tokens=max_tokens)
        except httpx.HTTPStatusError as e:
            console.print(f"[red]submit failed[/]: {e.response.status_code} "
                          f"{e.response.text[:200]}")
            raise typer.Exit(1) from e
        extra = f" session {sid}" if sid else ""
        console.print(f"[dim]task {task_id}{extra} submitted[/]")
        shown = 0
        while True:
            data = await client.task(task_id)
            partial = data.get("partial_output", "")
            if len(partial) > shown:
                console.print(partial[shown:], end="")
                shown = len(partial)
            if data["state"] in ("succeeded", "failed", "cancelled", "expired"):
                break
            await asyncio.sleep(0.25)
        if data["state"] == "succeeded":
            out = data["result"]["output"] or ""
            if len(out) > shown:
                console.print(out[shown:], end="")
            node = data.get("assigned_node") or "?"
            console.print(f"\n[green]done[/] on node {node[:8]} "
                          f"({data['result']['wall_time_s']:.1f}s)")
        else:
            console.print(f"\n[red]{data['state']}[/]: "
                          f"{(data.get('result') or {}).get('error')}")

    asyncio.run(run())


@app.command()
def task(task_id: str, server: str = typer.Option(None),
         cancel: bool = typer.Option(False, "--cancel")) -> None:
    """Inspect or cancel a task."""
    client = _resolve(server)

    async def run() -> None:
        if cancel:
            console.print({"cancelled": await client.cancel(task_id)})
        else:
            console.print(await client.task(task_id))

    asyncio.run(run())


@app.command()
def dash(server: str = typer.Option(None),
         once: bool = typer.Option(False, "--once",
                                   help="print one snapshot and exit"),
         interval: float = typer.Option(2.0, "--interval"),
         full: bool = typer.Option(False, "--full",
                                   help="fullscreen textual TUI "
                                   "(needs local install + tty)")) -> None:
    """Live terminal dashboard (headless-friendly, polls the server)."""
    client = _resolve(server)
    if full:
        from client.tui import DashboardApp
        asyncio.run(DashboardApp(client, interval=interval).run_dashboard())
        return
    import time
    import urllib.request
    base = client.base_url

    def fetch(path: str) -> str:
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.read().decode()
        except Exception as e:
            return f"{path}: unreachable ({e})"

    if once:
        console.print(fetch("/dash.txt"))
        return
    try:
        while True:
            console.clear() if hasattr(console, "clear") else None
            console.print(fetch("/dash.txt"))
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


@app.command()
def stats(server: str = typer.Option(None)) -> None:
    """Queue and node stats."""
    client = _resolve(server)

    async def run() -> None:
        console.print(await client.stats())

    asyncio.run(run())


def _authed(server: str | None) -> CliqueClient:
    """Client with a node token (registers this device's keypair)."""
    client = _resolve(server)
    asyncio.run(client.authenticate())
    return client


@app.command()
def sessions(server: str = typer.Option(None),
             create: bool = typer.Option(False, "--create"),
             cluster: str = typer.Option("", "--cluster"),
             show: str = typer.Option(None, "--show", help="session id"),
             migrate: str = typer.Option(None, "--migrate", help="session id"),
             to_node: str = typer.Option(None, "--to-node"),
             close: str = typer.Option(None, "--close", help="session id")) -> None:
    """List, create, inspect, migrate, or close sessions."""
    client = _authed(server)

    async def run() -> None:
        if create:
            console.print(await client.create_session(cluster))
        elif show:
            console.print(await client.session(show))
        elif migrate:
            if not to_node:
                raise typer.BadParameter("--migrate needs --to-node")
            console.print(await client.migrate_session(migrate, to_node))
        elif close:
            await client.close_session(close)
            client.forget_chat_session(close)
            console.print({"closed": True})
        else:
            table = Table(title="active sessions")
            for col in ("id", "cluster", "pinned", "version", "owner"):
                table.add_column(col)
            for s in await client.sessions():
                table.add_row(s["session_id"], s["cluster_key"] or "-",
                              (s["pinned_node"] or "-")[:8],
                              str(s["context_version"]),
                              s["owner_node"][:8])
            console.print(table)

    asyncio.run(run())


@app.command()
def op(target: str = typer.Argument(None, help="node id to op"),
       deop: str = typer.Option(None, "--deop", help="node id to deop"),
       policy: str = typer.Option(None, "--policy",
                                  help="first-client-op | open | democracy"),
       audit: bool = typer.Option(False, "--audit"),
       server: str = typer.Option(None)) -> None:
    """Permission control: /op, /deop, policy, audit, or list levels."""
    client = _authed(server)

    async def run() -> None:
        if target:
            console.print(await client.op(target))
        elif deop:
            console.print(await client.deop(deop))
        elif policy:
            console.print(await client.set_policy(policy))
        elif audit:
            for e in await client.audit():
                console.print(e)
        else:
            table = Table(title="permissions")
            for col in ("name", "id", "level"):
                table.add_column(col)
            for p in await client.permissions():
                table.add_row(p["display_name"], p["node_id"][:8], p["level"])
            console.print(table)

    asyncio.run(run())


@app.command()
def kick(node_id: str, server: str = typer.Option(None)) -> None:
    """Op: remove a node from the clique."""
    client = _authed(server)
    asyncio.run(client.kick(node_id))
    console.print(f"kicked {node_id}")


@app.command()
def cron(expr: str = typer.Option(None, "--request",
                                  help="cron expression, e.g. '0 * * * *'"),
         prompt: str = typer.Option(None, "--prompt"),
         approve: str = typer.Option(None, "--approve", help="cron id"),
         reject: str = typer.Option(None, "--reject", help="cron id"),
         disable: str = typer.Option(None, "--disable", help="cron id"),
         server: str = typer.Option(None)) -> None:
    """Cron jobs: request (any node), approve/reject (op), disable, list."""
    client = _authed(server)

    async def run() -> None:
        if expr:
            if not prompt:
                raise typer.BadParameter("--request needs --prompt")
            console.print(await client.cron_request(expr, prompt))
        elif approve:
            console.print(await client.cron_approve(approve))
        elif reject:
            console.print(await client.cron_reject(reject))
        elif disable:
            console.print(await client.cron_disable(disable))
        else:
            table = Table(title="cron jobs")
            for col in ("id", "expr", "requested by", "status", "last run"):
                table.add_column(col)
            for c in await client.cron_list():
                status = ("enabled" if c["enabled"]
                          else "disabled" if c["approved_by"] else "pending")
                table.add_row(c["cron_id"], c["cron_expr"],
                              c["requested_by"][:8], status,
                              c["last_run_at"] or "-")
            console.print(table)

    asyncio.run(run())


@app.command()
def suggestions(dismiss: str = typer.Option(None, "--dismiss"),
                server: str = typer.Option(None)) -> None:
    """View or dismiss model-change suggestions."""
    client = _authed(server)

    async def run() -> None:
        if dismiss:
            console.print(await client.dismiss_suggestion(dismiss))
            return
        items = await client.suggestions()
        if not items:
            console.print("[dim]no active suggestions[/]")
        for s in items:
            console.print(f"[bold]{s['suggestion_id']}[/] {s['rationale']}"
                          f"\n  [dim]{s['expected_effect']}[/]")

    asyncio.run(run())


@app.command()
def vcs(diff: str = typer.Option(None, "--diff", help="shaA..shaB"),
        rollback: str = typer.Option(None, "--rollback", help="sha"),
        server: str = typer.Option(None)) -> None:
    """State snapshot history, diff, and op-gated rollback."""
    client = _authed(server)

    async def run() -> None:
        if diff:
            a, _, b = diff.partition("..")
            console.print(await client.vcs_diff(a, b))
        elif rollback:
            console.print(await client.vcs_rollback(rollback))
        else:
            table = Table(title="state snapshots")
            for col in ("sha", "message", "actor", "at"):
                table.add_column(col)
            for c in await client.vcs_history():
                table.add_row(c["sha"][:10], c["message"], c["actor"],
                              c["timestamp"])
            console.print(table)

    asyncio.run(run())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
