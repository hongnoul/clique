"""Headless CLI. `clique help` prints the reference below verbatim.

Commands that talk to a clique take --server URL, falling back to
$CLIQUE_SERVER and then to mDNS discovery, so the flag is only needed
to override. The rest (status, logs, leave) are local-only.

  clique                               button home: Host Join Chat Dashboard
  clique ui                            the same home screen, explicit

run one:
  clique serve [--foreground]          start the server in the background
  clique join [--runtime ...]          join this device as a node, backgrounded
  clique onboard [--dry]               probe, auto-detect runtime, then join
  clique join-remote SSH_HOST          join a headless box over ssh
  clique leave / clique stop           disconnect this node / stop the server
  clique status                        what's running locally, on demand
  clique logs {server|join} [-f]       tail a backgrounded process's log

use it:
  clique submit PROMPT                 chat turn (sticky session by default)
  clique code-submit -p TEXT -f FILE   code edit, test-verified before commit
  clique task TASK_ID [--cancel]       inspect or cancel a task
  clique sessions [--show|--close ID]  list or manage chat sessions
  clique workspace --create|--watch    live shared workspaces

watch it:
  clique dash [--once|--full]          terminal dashboard + web UI links
  clique nodes / clique stats          nodes and clusters / queue and load
  clique ledger                        accepted-work accounting per node
  clique suggestions [--dismiss ID]    model-change suggestions
  clique vcs [--diff A..B]             state snapshot history and rollback

administer it (any joined node may):
  clique kick NODE_ID                  remove a node from the clique
  clique clear                         wipe sessions, queue, stored data
  clique mcp install|status|serve|grow clique tools inside agent harnesses

The web UI is served by the server node at <server>/dash (live) and
<server>/chat (ask it something); `clique dash` prints both links.

`serve` and `join` background themselves by default so one terminal can
run serve, then join, then submit/dash/etc. in sequence; pass
--foreground to block in the current terminal instead (e.g. under a
process supervisor).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from client.sdk import CliqueClient

app = typer.Typer(no_args_is_help=False, add_completion=False,
                   invoke_without_command=True)
console = Console()


@app.callback()
def _home_default(ctx: typer.Context,
                  server: str = typer.Option(
                      None, "--server",
                      help="server URL for the home screen")) -> None:
    """Seamless home: `clique` with no command opens the button TUI."""
    if ctx.invoked_subcommand is None:
        from client.home import main as home_main
        home_main(server)
        raise typer.Exit(0)


@app.command(name="help")
def help_cmd() -> None:
    """What every clique command does, grouped by what you came to do."""
    import re
    from rich.markup import escape

    for line in (__doc__ or "").strip().splitlines():
        entry = re.match(r"^(\s+)(clique\b.*?)(\s{2,})(\S.*)$", line)
        if entry:
            pad, cmd, gap, what = entry.groups()
            console.print(f"{pad}[bold cyan]{escape(cmd)}[/]{gap}"
                          f"[dim]{escape(what)}[/]")
        elif line.endswith(":"):
            console.print(f"[bold]{escape(line)}[/]")
        else:
            console.print(escape(line))


@app.command()
def ui(server: str = typer.Option(
        None, help="server URL (or $CLIQUE_SERVER)")) -> None:
    """Open the seamless home screen: Host / Join / Chat / Dashboard."""
    from client.home import main as home_main
    home_main(server)


def _resolve(server: str | None) -> CliqueClient:
    import os
    if not server:
        server = os.environ.get("CLIQUE_SERVER")
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
         base_url: str = typer.Option(None, help="openai-compat base URL, e.g. http://127.0.0.1:11434/v1"),
         param_b: float = typer.Option(None, help="model size in B params"),
         active_param_b: float = typer.Option(None, help="active params in B (MoE)"),
         parallel_slots: int = typer.Option(None, help="concurrent tasks the runtime can batch (vLLM: 8+)"),
         foreground: bool = typer.Option(
             False, "--foreground", "-f",
             help="block in this terminal instead of backgrounding")) -> None:
    """Join this device to the clique as a node. Backgrounds itself by default."""
    import typer as _typer

    def _val(v):
        # Direct calls (e.g. onboard -> join) bypass typer, so unset
        # options arrive as OptionInfo defaults instead of None/str.
        if v is None or isinstance(v, _typer.models.OptionInfo):
            return None
        return v

    server, name, runtime = _val(server), _val(name), _val(runtime)
    model_name, base_url = _val(model_name), _val(base_url)
    param_b = _val(param_b)
    active_param_b, parallel_slots = _val(active_param_b), _val(parallel_slots)
    foreground = v if isinstance((v := _val(foreground)), bool) else False
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
    if active_param_b:
        extra += ["--active-param-b", str(active_param_b)]
    if base_url:
        extra += ["--base-url", base_url]
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
        for col in ("name", "id", "role", "status", "model", "task"):
            table.add_column(col)
        table.add_row(
            clique_info.get("name", "server"), client.base_url.split("://", 1)[-1],
            "server", "up", "-", "-",
            style="bold cyan")
        for n in node_list:
            table.add_row(
                n.display_name, n.node_id[:8], n.role.value,
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
           workspace: str = typer.Option(None, "--workspace", help="live workspace id"),
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
                                          workspace_id=workspace,
                                          max_output_tokens=max_tokens)
        except httpx.HTTPStatusError as e:
            console.print(f"[red]submit failed[/]: {e.response.status_code} "
                          f"{e.response.text[:200]}")
            raise typer.Exit(1) from e
        extra = f" session {sid}" if sid else ""
        console.print(f"[dim]task {task_id}{extra} submitted[/]")
        view = None
        async for delta, done in client.stream_ws(task_id):
            if delta:
                console.print(delta, end="")
            if done is not None:
                view = done
        assert view is not None
        if view.state.value == "succeeded":
            node = view.assigned_node or "?"
            wall = view.result.wall_time_s or 0 if view.result else 0
            console.print(f"\n[green]done[/] on node {node[:8]} "
                          f"({wall:.1f}s)")
        else:
            err = view.result.error if view.result and view.result.error else "?"
            console.print(f"\n[red]{view.state.value}[/]: {err}")

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
                workspace: str = typer.Option(None, "--workspace",
                                             help="live workspace id for realtime collab"),
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
        # Stream live progress while the node generates (code tasks can
        # run for minutes: harness build + inference + test verify), then
        # show the verified diff on accept.
        view = None
        saw_delta = False
        async for delta, done in client.stream_ws(task_id):
            if delta:
                saw_delta = True
                console.print(delta, end="")
            if done is not None:
                view = done
        assert view is not None
        if saw_delta:
            console.print()  # newline after streamed progress
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
            session_id=session, workspace_id=workspace, use_tools=tools)
        console.print(f"[dim]code task {task_id} submitted[/]")
        await show_result(task_id)

    asyncio.run(run())


@app.command()
def ledger(server: str = typer.Option(None)) -> None:
    """Accepted-work accounting: per-node contributions and states."""
    client = _resolve(server)

    async def run() -> None:
        data = await client.ledger()
        table = Table(title="ledger (accepted contributions)")
        for col in ("node", "terminal", "accepted"):
            table.add_column(col)
        for row in data.get("by_node", []):
            table.add_row(str(row["node_id"])[:8], str(row["terminal"]),
                          str(row["accepted"]))
        console.print(table)
        console.print(f"accepted={data.get('accepted_tasks')} "
                      f"of {data.get('total_terminal')} terminal tasks")

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


def _browser_base(client: CliqueClient) -> str:
    """Base URL to hand a browser for this clique.

    Prefers the server's public tunnel when one is up, so the link also
    works from a phone that is not on the LAN; otherwise it is simply
    the address this CLI is already talking to.
    """
    base = client.base_url.rstrip("/")
    try:
        import json as _json
        import urllib.request as _url

        from common.tls import urlopen_kwargs
        with _url.urlopen(base + "/v1/clique", timeout=3,
                          **urlopen_kwargs(base)) as r:
            public = (_json.loads(r.read().decode()) or {}).get("public_url")
        if public:
            return str(public).rstrip("/")
    except Exception:
        pass  # server down or old: the address we dialed is still right
    return base


def _web_links(client: CliqueClient) -> str:
    """One line of clickable web UI links (terminals hyperlink OSC 8).

    Resolved once by the caller: the live dashboard redraws on a timer
    and should not re-probe the server just to restate its own address.
    """
    base = _browser_base(client)
    return (f"[dim]web[/] [link={base}/dash]{base}/dash[/link]"
            f"  [dim]·[/] [link={base}/chat]{base}/chat[/link]")


@app.command()
def dash(server: str = typer.Option(None),
         once: bool = typer.Option(False, "--once",
                                   help="print one snapshot and exit"),
         interval: float = typer.Option(2.0, "--interval"),
         full: bool = typer.Option(False, "--full",
                                   help="fullscreen textual TUI "
                                   "(needs local install + tty)")) -> None:
    """Live terminal dashboard, plus the web dashboard/chat links.

    The live view redraws in place and is left with `q` (or Ctrl+C),
    like `clique logs -f`. For the dashboard without a terminal at all,
    open the web link it prints, or take one snapshot with --once.
    """
    client = _resolve(server)
    links = _web_links(client)
    if full:
        console.print(links)
        from client.tui import DashboardApp
        asyncio.run(DashboardApp(client, interval=interval).run_dashboard())
        return
    import urllib.request
    base = client.base_url

    def fetch(path: str) -> str:
        try:
            from common.tls import urlopen_kwargs
            with urllib.request.urlopen(base + path, timeout=5,
                                        **urlopen_kwargs(base)) as r:
                return r.read().decode()
        except Exception as e:
            return f"{path}: unreachable ({e})"

    if once:
        console.print(fetch("/dash.txt"))
        console.print(links)
        return

    from client import daemon
    try:
        with daemon.quit_key_reader() as quit_pressed:
            while True:
                console.clear() if hasattr(console, "clear") else None
                console.print(fetch("/dash.txt"))
                console.print(links)
                console.print("[dim]-- live; q to stop "
                              "(Ctrl+C also works) --[/]")
                if quit_pressed(interval):  # doubles as the frame delay
                    break
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
def kick(node_id: str, server: str = typer.Option(None)) -> None:
    """Remove a node from the clique."""
    client = _authed(server)
    asyncio.run(client.kick(node_id))
    console.print(f"kicked {node_id}")


@app.command()
def clear(server: str = typer.Option(None),
          yes: bool = typer.Option(
              False, "--yes", "-y",
              help="don't prompt before wiping sessions, queue, and data")) -> None:
    """Delete all sessions, queued/historical tasks, and stored data.
    Joined nodes stay; the clique itself is not torn down."""
    if not (yes or typer.confirm(
            "Wipe every session, task, and stored workspace on this server?")):
        console.print("[dim]aborted[/]")
        raise typer.Exit(0)
    client = _authed(server)
    counts = asyncio.run(client.clear_data())
    client.forget_chat_session()
    console.print("[green]cleared[/]")
    console.print(counts)


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


@app.command()
def workspace(list: bool = typer.Option(False, "--list", help="list workspaces"),
              create: bool = typer.Option(False, "--create",
                                          help="create from files"),
              files: str = typer.Option(None, "--files",
                                        help="JSON dict path->content"),
              show: str = typer.Option(None, "--show",
                                       help="workspace id to snapshot"),
              file: str = typer.Option(None, "--file",
                                       help="path inside workspace"),
              flush: str = typer.Option(None, "--flush",
                                        help="force git checkpoint"),
              write: str = typer.Option(None, "--write",
                                        help="workspace id to write into "
                                             "(needs --file and --content "
                                             "or --from)"),
              content: str = typer.Option(None, "--content",
                                          help="literal new file content"),
              from_path: str = typer.Option(None, "--from",
                                            help="local file to upload as "
                                                 "the new content"),
              watch: str = typer.Option(None, "--watch",
                                        help="workspace id to stream live "
                                             "events from (Ctrl-C to stop)"),
              history: str = typer.Option(None, "--history",
                                          help="recent op log for workspace id"),
              commits: str = typer.Option(None, "--commits",
                                          help="git checkpoint log for workspace id"),
              server: str = typer.Option(None)) -> None:
    """Live realtime workspaces: create, list, snapshot, write, watch, flush."""
    import json as _json
    client = _authed(server)

    async def run() -> None:
        if create:
            data = await client.workspace_create(
                _json.loads(files) if files else {})
            console.print(f"[green]{data['workspace_id']}[/] seq={data['seq']}")
        elif write:
            if not file or (content is None and not from_path):
                console.print("[red]--write needs --file and --content/--from[/]")
                raise typer.Exit(2)
            text = content if content is not None \
                else Path(from_path).read_text()
            ev = await client.workspace_write(write, file, text)
            rb = " rebased" if ev.get("rebased") else ""
            console.print(f"[green]wrote[/] {file} v{ev['version']}"
                          f" seq={ev['seq']}{rb}")
        elif watch:
            async for ev in client.workspace_watch(watch):
                t = ev.get("type", "?")
                if t == "workspace.snapshot":
                    console.print(f"[dim]snapshot seq={ev['seq']} files="
                                  f"{list(ev.get('files', {}))}[/]")
                elif t == "workspace.delta":
                    rb = " [yellow]rebased[/]" if ev.get("rebased") else ""
                    console.print(f"seq={ev['seq']} {ev['path']}"
                                  f" v{ev['version']} by"
                                  f" {str(ev.get('actor', ''))[:8]}{rb}")
                else:
                    console.print(f"[dim]{t}[/] {ev}")
        elif history:
            for h in await client.workspace_history(history, path=file):
                rb = " [yellow]rebased[/]" if h["rebased"] else ""
                console.print(f"[dim]seq={h['seq']}[/] {h['path']}"
                              f" v{h['version']} by {h['actor'][:8]}{rb}")
        elif commits:
            for c in await client.workspace_commits(commits):
                console.print(f"[dim]{c['sha'][:10]}[/] {c['message']}")
        elif show:
            if file:
                st = await client.workspace_file(show, file)
                console.print(f"[bold]{file}[/] v{st['version']}"
                              f" seq={st['seq']}\n{st['text']}")
            else:
                snap = await client.workspace(show)
                for p, f in snap["files"].items():
                    console.print(f"[bold]{p}[/] v{f['version']}")
        elif flush:
            console.print(await client.workspace_flush(flush))
        else:
            for w in await client.workspaces():
                console.print(f"[bold]{w['workspace_id']}[/]"
                              f" seq={w['seq']}")

    asyncio.run(run())


@app.command(name="join-remote")
def join_remote(host: str = typer.Argument(..., help="ssh target, e.g. gx10 or user@host"),
                server: str = typer.Option(None, help="clique server URL (or $CLIQUE_SERVER)"),
                ssh_opt: list[str] = typer.Option(None, "--ssh-opt", help="extra ssh opt, repeatable")) -> None:
    """Join a headless box over ssh without exposing ssh to the UI.

    SSH/Tailscale is pure transport hidden here: this runs
    ``curl $SERVER/join.sh | sh`` on the remote and prints the join line.
    No tokens pasted, no GitHub involved.
    """
    import os as _os
    import subprocess as _sp
    srv = server or _os.environ.get("CLIQUE_SERVER")
    if not srv:
        raise typer.BadParameter("pass --server or set CLIQUE_SERVER")
    srv = srv if srv.startswith("http") else f"http://{srv}"
    cmd = ["ssh"]
    for o in (ssh_opt or []):
        cmd += ["-o", o]
    cmd += [host, f"curl -fsSL --connect-timeout 5 {srv}/join.sh | sh"]
    console.print(f"[dim]joining {host} via hidden ssh transport...[/]")
    rc = _sp.call(cmd)
    if rc != 0:
        raise typer.Exit(rc)
    console.print(f"[green]installed on {host}[/]; then: ssh {host} "
                  f"'clique join --server {srv} --runtime echo --param-b 7'")


@app.command()
def onboard(server: str = typer.Option(None, help="clique server URL (or $CLIQUE_SERVER)"),
            runtime: str = typer.Option(None, help="echo | openai-compat (auto-detected if omitted)"),
            model_name: str = typer.Option(None, help="openai-compat model, e.g. qwen2.5-coder:7b"),
            base_url: str = typer.Option(None, help="openai-compat base URL, e.g. http://127.0.0.1:11434/v1"),
            param_b: float = typer.Option(None, help="model size in B params"),
            parallel_slots: int = typer.Option(None, help="concurrent tasks the runtime can batch (vLLM: 8+)"),
            dry: bool = typer.Option(False, "--dry", help="print what would happen, do not join")) -> None:
    """Graceful dogfood onboarding: probe server, detect runtime, warn on mismatch, join.

    1. Probes $SERVER/v1/clique (reachability + protocol_version + server_sha).
    2. Auto-detects a local runtime when --runtime is omitted: probes
       --base-url (or ollama :11434, then llama-server :8080) /v1/models.
    3. Warns (never blocks) when the local checkout SHA differs from
       server_sha, so mixed-version dogfood nodes are visible.
    4. Joins with the resolved runtime via ``clique join``.
    """
    import json as _json
    import os as _os
    import urllib.request as _url
    srv = server or _os.environ.get("CLIQUE_SERVER")
    if not srv:
        raise typer.BadParameter("pass --server or set CLIQUE_SERVER")
    srv = srv if srv.startswith("http") else f"http://{srv}"

    def _get(path: str, base: str = srv, timeout: float = 5.0) -> dict | None:
        try:
            from common.tls import urlopen_kwargs
            with _url.urlopen(base + path, timeout=timeout,
                              **urlopen_kwargs(base)) as r:
                return _json.loads(r.read().decode())
        except Exception:
            return None

    info = _get("/v1/clique")
    if info is None:
        console.print(f"[red]server unreachable[/]: {srv}/v1/clique timed out.\n"
                      f"Check tailscale/VPN, then retry.")
        raise typer.Exit(1)
    from common import protocol as _proto
    sproto = info.get("protocol_version")
    if sproto is not None and sproto != _proto.PROTOCOL_VERSION:
        console.print(f"[yellow]warn[/]: server protocol={sproto} "
                      f"!= local protocol={_proto.PROTOCOL_VERSION}. "
                      f"Update from {srv}/app.tgz if tasks fail.")
    ssha = info.get("server_sha")
    local_sha = _local_sha()
    if ssha and local_sha and ssha not in ("unknown",) and local_sha != ssha:
        console.print(f"[yellow]warn[/]: server tree {ssha} != local {local_sha}. "
                      f"Graceful mode: joining anyway; refresh with "
                      f"`curl -fsSL {srv}/app.tgz` if behavior drifts.")
    console.print(f"[green]server ok[/]: {info.get('name')} "
                  f"(protocol={sproto}, sha={ssha})")

    rt = runtime
    bu = base_url
    mn = model_name
    if rt is None:
        rt, bu, mn = _detect_runtime(bu, mn, _get)
        console.print(f"[dim]detected runtime: {rt}"
                      f"{f' {mn}' if mn else ''}"
                      f"{f' @ {bu}' if bu else ''}[/]")
    if rt == "openai-compat" and not (bu and mn):
        console.print("[red]openai-compat needs --base-url and --model-name[/] "
                      "(or a live ollama/llama-server to auto-detect).")
        raise typer.Exit(2)
    console.print(f"joining {srv} as [bold]{rt}[/]"
                  f"{f' (slots={parallel_slots})' if parallel_slots else ''}...")
    if dry:
        console.print("[dim]--dry: not joining[/]")
        return
    join(server=srv, runtime=rt, model_name=mn, base_url=bu,
         param_b=param_b, parallel_slots=parallel_slots, foreground=True)


def _local_sha() -> str | None:
    """Short SHA of the local checkout, or None when unavailable."""
    import contextlib as _cl
    import subprocess as _sp
    from pathlib import Path as _Path
    with _cl.suppress(Exception):
        root = _Path(__file__).resolve().parents[1]
        sha = _sp.check_output(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            timeout=5, stderr=_sp.DEVNULL).decode().strip()
        if sha:
            return sha
    return None


def _detect_runtime(base_url: str | None, model_name: str | None,
                    get) -> tuple[str, str | None, str | None]:
    """Pick echo vs openai-compat by probing local /v1/models endpoints."""
    candidates = ([base_url] if base_url else []) + [
        "http://127.0.0.1:11434/v1",  # ollama
        "http://127.0.0.1:8080/v1",   # llama-server
    ]
    for bu in candidates:
        models = get("/models", base=bu, timeout=2.0)
        if isinstance(models, dict):
            data = models.get("data") or []
            name = model_name
            if name is None and data and isinstance(data[0], dict):
                name = data[0].get("id")
            if name:
                return "openai-compat", bu, name
            if model_name:
                return "openai-compat", bu, model_name
    return "echo", None, None


mcp_app = typer.Typer(no_args_is_help=True, add_completion=False,
                      help="MCP integration for local agent harnesses.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("install")
def mcp_install(url: str = typer.Option(
        None, help="pin CLIQUE_URL (omit to use LAN discovery)"),
        show_skipped: bool = typer.Option(
            False, "--show-skipped", help="also list absent harnesses")) -> None:
    """Register clique-mcp with every agent harness on this machine.

    Headless and idempotent: finds Claude Code, Codex, Cursor, Windsurf,
    Claude Desktop and Jcode configs, and adds/updates a ``clique`` stdio
    entry pointing at the absolute clique-mcp binary.
    """
    from client.mcp_install import find_binary, install
    binary = find_binary()
    results = install(url=url, binary=binary)
    touched = 0
    for r in results:
        if r.action.startswith("skipped"):
            if show_skipped:
                console.print(f"[dim]{r.harness:14} {r.action}[/]")
            continue
        touched += 1
        console.print(f"[green]{r.harness:14} {r.action}[/] -> {r.path}")
    console.print(f"\nbinary: {binary}")
    console.print(f"server: {url or 'mDNS discovery at call time'}")
    if touched == 0:
        console.print("[yellow]no harness configs found; "
                      "run with --show-skipped to see paths checked[/]")
    else:
        console.print("[dim]restart harnesses to pick up the change[/]")


@mcp_app.command("status")
def mcp_status() -> None:
    """Show which harnesses have the clique MCP server registered."""
    from client.mcp_install import status as mcp_st
    for r in mcp_st():
        color = {"registered": "green",
                 "absent": "dim"}.get(r.action, "yellow")
        console.print(f"[{color}]{r.harness:14} {r.action}[/] {r.path}")


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run the MCP server on stdio (what harnesses invoke)."""
    from client.mcp_server import main as mcp_main
    mcp_main()


@mcp_app.command("grow")
def mcp_grow(url: str = typer.Option(
        None, help="pin CLIQUE_URL (omit to use LAN discovery)"),
        no_check: bool = typer.Option(
            False, "--no-check",
            help="skip the smoke validation probe")) -> None:
    """Register everything in ~/dogfood-mcp as MCP servers.

    Convention for agent-built tools: each ``~/dogfood-mcp/<name>/``
    holds ``server.py`` (MCP stdio) plus an optional ``mcp.json``
    overriding {command, args, env}. Valid servers get a named entry
    in every harness config on this machine, so the next pane (jcode,
    claude, codex) starts with the accumulated capabilities.
    Idempotent: re-run any time. ``clique`` itself re-registers first.
    """
    from client.mcp_install import dogfood_dir, grow
    servers, results = grow(url=url, check=not no_check)
    console.print(f"[dim]dogfood dir: {dogfood_dir()}[/]")
    if not servers:
        console.print("[dim]no grown servers yet: agents add "
                      "~/dogfood-mcp/<name>/server.py, then re-run grow[/]")
    for s in servers:
        if s.valid:
            console.print(f"[green]{s.name:20} valid[/] {s.path}")
        else:
            console.print(f"[red]{s.name:20} skipped[/] {s.problem}")
    for r in results:
        if r.action.startswith("skipped"):
            continue
        color = "green" if r.action in ("installed", "updated") else "yellow"
        console.print(f"[{color}]{r.harness:24} {r.action}[/]")
    console.print("[dim]restart harnesses (or open a new pane) "
                  "to pick up the change[/]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
