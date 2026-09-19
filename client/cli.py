"""Headless CLI (MVP implementation).

Commands:
  clique serve                       run the server node here
  clique join [--server URL] ...     run an agent on this device
  clique nodes [--server URL]        list nodes and clusters
  clique submit PROMPT               submit a task and stream to done
  clique task TASK_ID [--cancel]     inspect or cancel a task
  clique stats                       queue and node stats
"""

from __future__ import annotations

import asyncio

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
           model: str = typer.Option(None, "--model", help="model hint (cluster key prefix)"),
           max_tokens: int = typer.Option(1024)) -> None:
    """Submit a task and wait for the result."""
    client = _resolve(server)

    async def run() -> None:
        task_id = await client.submit(prompt, model_hint=model,
                                      max_output_tokens=max_tokens)
        console.print(f"[dim]task {task_id} submitted[/]")
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
def stats(server: str = typer.Option(None)) -> None:
    """Queue and node stats."""
    client = _resolve(server)

    async def run() -> None:
        console.print(await client.stats())

    asyncio.run(run())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
