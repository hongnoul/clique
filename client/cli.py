"""Headless CLI (MVP implementation).

Commands:
  clique serve [--foreground]        start the server in the background
  clique join [--server URL] ...     join this device as a node, in the background
  clique status                      what's running locally (server/agent), on demand
  clique logs {server|join} [-f]     tail a backgrounded process's log
  clique stop / clique leave         gracefully stop the server / disconnect the node
  clique nodes [--server URL]        list nodes and clusters
  clique dash [--server URL] [--full]  live terminal dashboard (polls /dash.txt)
  clique submit PROMPT               chat turn (sticky session by default)
  clique task TASK_ID [--cancel]     inspect or cancel a task
  clique stats                       queue and node stats

`serve` and `join` background themselves by default so one terminal can
run serve, then join, then submit/dash/etc. in sequence; pass
--foreground to block in the current terminal instead (e.g. under a
process supervisor).
"""

from __future__ import annotations

import asyncio
import sys

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
def serve(port: int = typer.Option(None, help="override server.api_port"),
         host: str = typer.Option(None, help="override server.api_host"),
         no_announce: bool = typer.Option(False, "--no-announce"),
         foreground: bool = typer.Option(
             False, "--foreground", "-f",
             help="block in this terminal instead of backgrounding")) -> None:
    """Run the clique server. Backgrounds itself by default."""
    from common.config import load as load_config
    config = load_config()

    extra = []
    if port:
        extra += ["--port", str(port)]
    if host:
        extra += ["--host", host]
    if no_announce:
        extra += ["--no-announce"]

    if foreground:
        from scheduler.server import main as server_main
        sys.argv = ["clique-server"] + extra
        server_main()
        return

    from client import daemon
    argv = [sys.executable, "-m", "scheduler.server"] + extra
    bind_host = host or config.server.api_host
    bind_port = port or config.server.api_port
    try:
        info = daemon.spawn(config.node.data_dir, "server", argv,
                            meta={"host": bind_host, "port": bind_port})
    except daemon.SpawnError as e:
        _report_spawn_failure(str(e), e.log_tail)
        raise typer.Exit(1) from None
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/] (`clique status` for details)")
        return
    console.print(
        f"[green]server started[/] (pid {info.pid}) on {bind_host}:{bind_port}\n"
        f"  [dim]clique status   ·   clique logs server -f   ·   clique stop[/]")


def _report_spawn_failure(message: str, log_tail: str) -> None:
    console.print(f"[red]{message}[/]")
    if log_tail:
        console.print("[dim]--- last output ---[/]")
        console.print(log_tail[-2000:])


@app.command()
def join(server: str = typer.Option(None, help="server URL, skips mDNS"),
         name: str = typer.Option(None, help="display name, defaults to hostname"),
         runtime: str = typer.Option(None, help="echo | openai-compat"),
         model_name: str = typer.Option(None, help="e.g. qwen2.5-coder:7b"),
         param_b: float = typer.Option(None, help="model size in B params"),
         parallel_slots: int = typer.Option(None, help="concurrent tasks the runtime can batch (vLLM: 8+)"),
         foreground: bool = typer.Option(
             False, "--foreground", "-f",
             help="block in this terminal instead of backgrounding")) -> None:
    """Join this device to the clique as a node. Backgrounds itself by default."""
    from common.config import load as load_config
    from node.agent import resolve_server
    config = load_config()
    resolved = asyncio.run(resolve_server(server))

    extra = ["--server", resolved]
    if name:
        extra += ["--name", name]
    if runtime:
        extra += ["--runtime", runtime]
    if model_name:
        extra += ["--model-name", model_name]
    if param_b:
        extra += ["--param-b", str(param_b)]
    if parallel_slots:
        extra += ["--parallel-slots", str(parallel_slots)]

    if foreground:
        from node.agent import main as agent_main
        sys.argv = ["clique-agent"] + extra
        agent_main()
        return

    from client import daemon
    argv = [sys.executable, "-m", "node.agent"] + extra
    display_name = name or config.node.display_name
    try:
        info = daemon.spawn(config.node.data_dir, "agent", argv,
                            meta={"server": resolved, "name": display_name})
    except daemon.SpawnError as e:
        _report_spawn_failure(str(e), e.log_tail)
        raise typer.Exit(1) from None
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/] (`clique status` for details)")
        return
    console.print(
        f"[green]joined[/] {resolved} (pid {info.pid}) as {display_name}\n"
        f"  [dim]clique status   ·   clique logs join -f   ·   clique leave[/]")


@app.command()
def status() -> None:
    """What's running locally right now: pull-based, no polling."""
    from client import daemon
    from common.config import load as load_config
    config = load_config()
    data_dir = config.node.data_dir

    srv = daemon.status(data_dir, "server")
    if srv:
        console.print(f"[green]server[/]  running  pid={srv.pid}  "
                      f"{srv.meta.get('host', '?')}:{srv.meta.get('port', '?')}  "
                      f"since {srv.started_at}")
    else:
        console.print("[dim]server   not running[/]   (start with `clique serve`)")

    agent = daemon.status(data_dir, "agent")
    if agent:
        console.print(f"[green]agent[/]   running  pid={agent.pid}  "
                      f"-> {agent.meta.get('server', '?')}  "
                      f"as {agent.meta.get('name', '?')}  since {agent.started_at}")
    else:
        console.print("[dim]agent    not joined[/]   (join with `clique join`)")


@app.command()
def logs(role: str = typer.Argument(..., help="server | join"),
         follow: bool = typer.Option(False, "--follow", "-f"),
         lines: int = typer.Option(40, "-n", help="lines of history to print")) -> None:
    """Tail a backgrounded process's log file, on demand."""
    from client import daemon
    from common.config import load as load_config
    config = load_config()
    name = "agent" if role in ("join", "agent") else "server"
    daemon.tail(config.node.data_dir, name, lines=lines, follow=follow)


@app.command()
def leave(force: bool = typer.Option(
        False, "--force", help="skip the drain/LEAVE handshake, kill immediately")) -> None:
    """Gracefully disconnect the backgrounded joined node."""
    from client import daemon
    from common.config import load as load_config
    config = load_config()
    if not daemon.stop(config.node.data_dir, "agent", force=force):
        console.print("[dim]not joined[/]")
        return
    console.print("[yellow]force-quit[/]" if force else "[green]left the clique[/]")


@app.command()
def stop(server: str = typer.Option(
             None, help="server to stop (default: local server, else mDNS)"),
         yes: bool = typer.Option(
             False, "--yes", "-y",
             help="don't prompt when nodes have active tasks"),
         local: bool = typer.Option(
             False, "--local",
             help="skip the network call; signal the local server process "
             "directly (only works on the machine running it; use if it's "
             "unresponsive)"),
         force: bool = typer.Option(
             False, "--force",
             help="with --local, skip graceful shutdown and kill immediately")) -> None:
    """Stop the server -- from any node, not just the one running it.
    Any joined agents are told to disconnect too. Warns and asks for
    confirmation first if a node is actively running a task."""
    from common.config import load as load_config
    config = load_config()

    if local:
        from client import daemon
        if not daemon.stop(config.node.data_dir, "server", force=force):
            console.print("[dim]not running[/]")
            return
        console.print("[yellow]server force-killed[/]" if force else "[green]server stopped[/]")
        return

    import httpx

    target = server
    if not target:
        from client import daemon
        local_info = daemon.status(config.node.data_dir, "server")
        if local_info:
            target = f"http://127.0.0.1:{local_info.meta.get('port', config.server.api_port)}"
    if not target:
        from node.agent import resolve_server
        try:
            target = asyncio.run(resolve_server(None))
        except SystemExit as e:
            console.print(f"[yellow]{e}[/]")
            raise typer.Exit(1) from None

    client = CliqueClient(target)

    async def do_stop() -> dict:
        await client.authenticate(config.node.data_dir)
        try:
            return await client.server_shutdown(confirm=False)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                console.print(f"[red]not authorized:[/] {e.response.text}")
                raise typer.Exit(1) from None
            if e.response.status_code != 409:
                raise
            active = e.response.json().get("detail", {}).get("active", [])
            console.print(f"[yellow]{len(active)} node(s) are actively running a task:[/]")
            for a in active:
                console.print(f"  {a['display_name']} ({a['node_id'][:8]}) "
                             f"-> task {a['task_id']}")
            if not (yes or typer.confirm("Stop the server and kill these tasks anyway?")):
                console.print("[dim]aborted[/]")
                raise typer.Exit(0) from None
            return await client.server_shutdown(confirm=True)

    try:
        result = asyncio.run(do_stop())
    except (httpx.ConnectError, httpx.ConnectTimeout):
        console.print(
            f"[yellow]could not reach {target}[/] (already stopped? "
            "`clique status`, or `clique stop --local` if you're on its own machine)")
        raise typer.Exit(1) from None
    console.print(f"[green]server stopping[/] "
                  f"({result.get('active_tasks_killed', 0)} active task(s) killed)")


@app.command()
def nodes(server: str = typer.Option(None)) -> None:
    """List nodes and clusters. The server itself is always shown first."""
    client = _resolve(server)

    async def run() -> None:
        node_list = await client.nodes()
        clique_info = await client.clique()
        table = Table(title="clique nodes")
        for col in ("name", "id", "role", "op", "status", "model", "task"):
            table.add_column(col)
        table.add_row(
            clique_info.get("name", "server"), client.base_url.split("://", 1)[-1],
            "server", "-", "up", "-", "-",
            style="bold cyan")
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
def code_submit(prompt: str = typer.Option(..., "--prompt", "-p"),
                file: list[str] = typer.Option(
                    None, "--file", "-f",
                    help="path=localfile, repeatable (path is repo-relative)"),
                inline: list[str] = typer.Option(
                    None, "--inline", help="path:content, repeatable"),
                test: str = typer.Option("pytest -q", "--test",
                                         help="test command, space-separated"),
                session: str = typer.Option(None, "--session"),
                tools: bool = typer.Option(
                    False, "--tools",
                    help="node-local tool loop (capable models only)"),
                race: int = typer.Option(
                    0, "--race",
                    help="fan out to N parallel proposals, first accept wins"),
                server: str = typer.Option(None)) -> None:
    """Submit a CODE_EDIT task from files and wait for verified result."""
    import shlex
    client = _resolve(server)
    files: dict[str, str] = {}
    for spec in (file or []):
        repo_path, _, local = spec.partition("=")
        if not repo_path or not local:
            raise typer.BadParameter("--file needs path=localfile")
        files[repo_path] = open(local).read()
    for spec in (inline or []):
        repo_path, _, content = spec.partition(":")
        if not repo_path:
            raise typer.BadParameter("--inline needs path:content")
        files[repo_path] = content.replace("\\n", "\n")
    if not files:
        raise typer.BadParameter("give at least one --file or --inline")

    async def show_result(task_id: str) -> None:
        view = await client.wait(task_id)
        if view.state.value == "succeeded":
            diff = await client.code_diff(task_id)
            console.print("[green]accepted[/]",
                          f"{task_id} sha={diff.get('applied_sha')}")
            console.print(diff.get("patch") or "")
        else:
            console.print(f"[red]{view.state.value}[/] {task_id}: "
                          f"{(view.result.error if view.result else '?')}")
            tests = await client.code_tests(task_id)
            if tests.get("test_report"):
                console.print(tests["test_report"])

    async def run() -> None:
        if race > 0:
            body = await client.code_race(
                prompt, files, fanout=race, test_cmd=shlex.split(test))
            console.print(f"[dim]race {body['race_id']}: "
                          f"{body['task_ids']}[/]")
            for tid in body["task_ids"]:
                await show_result(tid)
            return
        task_id = await client.code_submit(
            prompt, files, test_cmd=shlex.split(test),
            session_id=session, use_tools=tools)
        console.print(f"[dim]code task {task_id} submitted[/]")
        await show_result(task_id)

    asyncio.run(run())


@app.command()
def ledger(server: str = typer.Option(None)) -> None:
    """Accepted-work accounting: totals, per-node earnings, states."""
    client = _resolve(server)

    async def run() -> None:
        data = await client.ledger()
        table = Table(title="ledger (run-rate projection, not payout)")
        for col in ("node", "terminal", "accepted", "earned"):
            table.add_column(col)
        for row in data.get("by_node", []):
            table.add_row(str(row["node_id"])[:8], str(row["terminal"]),
                          str(row["accepted"]), f"${row['earned']:.2f}")
        console.print(table)
        console.print(f"accepted={data.get('accepted_tasks')} "
                      f"earned=${data.get('earned_run_rate', 0):.2f} "
                      f"@ ${data.get('rate_per_task', 0.20):.2f}/task")

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
