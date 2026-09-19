"""Tests for the zero-install curl TUI flow (headless access).

Exercises the plain-text endpoints live over HTTP against a real
uvicorn server: the / menu, /dash.txt snapshot, /tui.py stdlib-only
source, and /join.sh installer. render_lines is also unit-tested
directly so empty and populated snapshots both render sane text.

Note: these tests spawn a live server instead of using TestClient
because Registry/Router hold same-thread sqlite connections. And
blocking urllib calls run in threads (asyncio.to_thread) because the
server shares this event loop: a sync urlopen would starve it.
"""

from __future__ import annotations

import ast
import asyncio
import socket
from pathlib import Path
import urllib.request

import pytest_asyncio
import uvicorn

from client.curl_tui import render_lines, snapshot
from common.config import Config
from scheduler.server import SchedulerServer

STDLIB_ONLY = {"argparse", "json", "os", "sys", "time", "curses",
               "urllib.error", "urllib.request", "__future__"}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def headless_server(tmp_path):
    """Live server with no agents joined (empty-clique view)."""
    import httpx

    port = free_port()
    cfg = Config()
    cfg.server.api_host = "127.0.0.1"
    cfg.server.api_port = port
    cfg.server.db_path = tmp_path / "server.db"
    server = SchedulerServer(cfg)
    uv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1",
                                       port=port, log_level="warning"))
    task = asyncio.create_task(uv.serve())
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            try:
                await c.get(base + "/v1/clique")
                break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)
    yield base
    uv.should_exit = True
    await asyncio.gather(task, return_exceptions=True)


async def afetch(base: str, path: str) -> tuple[str, str]:
    """GET via plain urllib (the real headless path), in a thread so the
    in-loop server can still answer. Returns (body, content-type)."""

    def _do() -> tuple[str, str]:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.read().decode(), r.headers.get("content-type", "")

    return await asyncio.to_thread(_do)


async def test_index_menu_lists_all_flows(headless_server):
    body, _ = await afetch(headless_server, "/")
    for needle in ("/dash.txt", "/tui.py", "/join.sh"):
        assert needle in body, f"menu missing {needle}"
    # /join is the spec'd alias of / (rest.py join assets)
    alias, _ = await afetch(headless_server, "/join")
    assert alias == body


async def test_dash_txt_snapshot(headless_server):
    body, ctype = await afetch(headless_server, "/dash.txt")
    assert "text/plain" in ctype
    assert "clique:" in body
    assert "NODES (0)" in body
    assert "TASKS" in body
    # non-interactive footer: dash.txt has no key handling
    assert "[q] quit" not in body


async def test_tui_py_is_stdlib_only(headless_server):
    src, _ = await afetch(headless_server, "/tui.py")
    tree = ast.parse(src)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    assert imports <= STDLIB_ONLY, f"non-stdlib imports: {imports - STDLIB_ONLY}"
    # served source must be runnable: compile + --once against live server
    compile(src, "/tui.py", "exec")
    assert "def render_lines" in src
    assert "def main" in src


async def test_join_sh_pins_server(headless_server, tmp_path):
    body, _ = await afetch(headless_server, "/join.sh")
    assert headless_server in body
    assert "GIT_TERMINAL_PROMPT=0" in body  # headless-safe, no prompts
    assert "CLIQUE_GITHUB_TOKEN" in body  # private-repo support ships too
    assert body.startswith("#!/bin/sh")
    assert body.count("#!/bin/sh") == 1  # no doubled shebang
    assert body.count('echo "installed:') == 1  # no duplicated footer
    # parity: installer logic lives in scripts/bootstrap.sh, served verbatim
    disk = (Path(__file__).resolve().parents[1] / "scripts"
            / "bootstrap.sh").read_text()
    for line in ("fetch_tarball() {", "diag_clone_failure() {",
                 'export GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"'):
        assert line in disk, f"bootstrap.sh lost: {line}"
        assert line in body, f"join.sh diverged from bootstrap.sh: {line}"
    # served script is valid POSIX sh
    import subprocess
    script = tmp_path / "join-served.sh"
    script.write_text(body)
    proc = await asyncio.to_thread(
        subprocess.run, ["sh", "-n", str(script)],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"served join.sh fails sh -n: {proc.stderr}"


def test_bootstrap_sh_is_posix_clean():
    import subprocess
    src = (Path(__file__).resolve().parents[1] / "scripts"
           / "bootstrap.sh").read_text()
    assert "GIT_TERMINAL_PROMPT=0" in src
    assert "CLIQUE_GITHUB_TOKEN" in src
    assert "$'" not in src  # no bashisms: runs under POSIX sh
    proc = subprocess.run(
        ["sh", "-n", str(Path(__file__).resolve().parents[1]
                         / "scripts" / "bootstrap.sh")],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"bootstrap.sh fails sh -n: {proc.stderr}"


async def test_snapshot_and_render_empty(headless_server):
    snap = await asyncio.to_thread(snapshot, headless_server)
    assert isinstance(snap["nodes"], list) and snap["nodes"] == []
    lines = render_lines(snap)
    text = "\n".join(lines)
    assert "none joined yet" in text
    assert "TASKS" in text


def test_render_lines_populated():
    snap = {
        "clique": {"name": "demo", "policy": "first-client-op",
                   "default_model": "qwen2.5-coder-7b"},
        "nodes": [{
            "display_name": "mac-1", "status": "ready",
            "op_level": "op", "current_task_id": "t-abc123",
            "model": {"family": "qwen", "parameter_count_b": 7.0},
        }],
        "clusters": [{"cluster_key": "qwen-7b-none", "node_ids": ["n1"]}],
        "stats": {"by_state": {"queued": 1}},
        "tasks": [{
            "request": {"task_id": "t-abc123", "prompt": "write fizzbuzz"},
            "state": "queued", "assigned_node": None,
        }],
    }
    text = "\n".join(render_lines(snap))
    assert "mac-1" in text
    assert "write fizzbuzz" in text
    assert "qwen-7b-none" in text
    assert "queued" in text


def test_render_lines_unreachable():
    snap = {"clique": {"_error": "/v1/clique: refused"},
            "nodes": {"_error": "/v1/nodes: refused"},
            "clusters": {"_error": "x"}, "stats": {"_error": "x"},
            "tasks": {"_error": "x"}}
    text = "\n".join(render_lines(snap))
    assert "unreachable" in text


def test_summarize_flattens_snapshot():
    from client.tui import summarize

    snap = {
        "nodes": [{"display_name": "n1", "status": "ready", "op_level": "op",
                   "current_task_id": None,
                   "model": {"family": "q", "parameter_count_b": 7}}],
        "tasks": [{"request": {"task_id": "t-1", "prompt": "hi"},
                   "state": "queued", "assigned_node": None}],
        "stats": {"by_state": {"queued": 1}},
        "clusters": [{"cluster_key": "q-7b", "node_ids": ["a"]}],
    }
    rows = summarize(snap)
    assert rows["nodes"] == [("n1", "ready", "q-7", "op", "-")]
    assert rows["tasks"] == [("t-1", "queued", "-", "hi")]
    assert rows["queue"] == [("queued", "1")]
    assert rows["clusters"] == [("q-7b", "1")]


async def test_dashboard_app_boots_headless():
    """Real Textual app boots in headless pilot mode with 4 tabs and
    degrades to 'unreachable' instead of crashing (nothing listening)."""
    from textual.widgets import TabbedContent

    from client.sdk import CliqueClient
    from client.tui import DashboardApp

    app = DashboardApp(CliqueClient("http://127.0.0.1:1"), interval=60.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        assert [t.id for t in tabs.query("TabPane")] == [
            "devices", "queue", "sessions", "governance"]
        await app.refresh_data()
        assert "unreachable" in str(app.query_one("#status").content)


async def test_served_tui_runs_once_as_subprocess(headless_server,
                                                  tmp_path):
    """Closest thing to the advertised one-liner: fetch /tui.py and run
    it with plain `python3 ... --once` in a subprocess (no venv, no
    deps) against the live server."""
    import subprocess
    import sys

    src, _ = await afetch(headless_server, "/tui.py")
    script = tmp_path / "clique-tui.py"
    script.write_text(src)
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(script), "--server", headless_server,
         "--once"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "clique:" in proc.stdout
    assert "NODES (0)" in proc.stdout


async def test_cli_dash_once(headless_server, capsys):
    """`clique dash --once --server URL` prints the /dash.txt snapshot."""
    from typer.testing import CliRunner

    from client.cli import app

    result = await asyncio.to_thread(
        CliRunner().invoke, app,
        ["dash", "--server", headless_server, "--once"],
    )
    assert result.exit_code == 0, result.output
    assert "clique:" in result.output
