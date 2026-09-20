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
    for needle in ("/dash.txt", "/tui.py", "/join.sh", "/repo.bundle",
                   "clique              # Host Join Chat Dashboard buttons"):
        assert needle in body, f"menu missing {needle}"
    # no-GitHub flow: join needs no PAT, source comes from /app.tgz
    assert "no token needed" in body
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
    # no-GitHub flow: bundle comes from /app.tgz, no git prompts or PATs
    assert "app.tgz" in body
    assert "press Join" in body  # seamless home advertised after install
    assert "CLIQUE_GITHUB_TOKEN" not in body
    assert "GIT_TERMINAL_PROMPT" not in body
    assert body.startswith("#!/bin/sh")
    assert body.count("#!/bin/sh") == 1  # no doubled shebang
    assert "ready." in body  # herdr-style ready line, exactly once
    assert body.count("ready.") == 1  # no duplicated footer
    # served script is valid POSIX sh
    import subprocess
    script = tmp_path / "join-served.sh"
    script.write_text(body)
    proc = await asyncio.to_thread(
        subprocess.run, ["sh", "-n", str(script)],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"served join.sh fails sh -n: {proc.stderr}"


async def test_clique_reports_protocol_and_sha(headless_server):
    import json as _json
    import urllib.request as _url

    def _do() -> dict:
        with _url.urlopen(headless_server + "/v1/clique", timeout=10) as r:
            return _json.loads(r.read().decode())

    info = await asyncio.to_thread(_do)
    from common import protocol as _proto
    assert info["protocol_version"] == _proto.PROTOCOL_VERSION
    assert isinstance(info.get("server_sha"), str) and info["server_sha"]


async def test_repo_bundle_serves_git_history(headless_server):
    import urllib.request as _url

    def _do() -> tuple[bytes, str]:
        with _url.urlopen(headless_server + "/repo.bundle", timeout=60) as r:
            return r.read(), r.headers.get("content-type", "")

    blob, ctype = await asyncio.to_thread(_do)
    assert len(blob) > 1000
    # git bundle v2/v3 header, or gzip fallback (tarball snapshot)
    assert blob.startswith(b"# v") or blob[:2] == b"\x1f\x8b", blob[:40]


def test_detect_runtime_prefers_live_backend():
    from client.cli import _detect_runtime

    def fake_get(path, base=None, timeout=5.0):
        if base == "http://127.0.0.1:11434/v1":
            return {"data": [{"id": "qwen2.5-coder:7b"}]}
        return None

    rt, bu, mn = _detect_runtime(None, None, fake_get)
    assert (rt, bu, mn) == ("openai-compat", "http://127.0.0.1:11434/v1",
                            "qwen2.5-coder:7b")
    rt2, _, _ = _detect_runtime(None, None, lambda *a, **k: None)
    assert rt2 == "echo"


async def test_onboard_dry_run_and_unreachable(headless_server, capsys):
    from typer.testing import CliRunner

    from client.cli import app

    ok = await asyncio.to_thread(
        CliRunner().invoke, app,
        ["onboard", "--server", headless_server, "--runtime", "echo",
         "--dry"],
    )
    assert ok.exit_code == 0, ok.output
    assert "server ok" in ok.output

    bad = await asyncio.to_thread(
        CliRunner().invoke, app,
        ["onboard", "--server", "http://127.0.0.1:1", "--runtime", "echo",
         "--dry"],
    )
    assert bad.exit_code == 1
    assert "unreachable" in bad.output


def test_bootstrap_sh_is_posix_clean():
    import subprocess
    src = (Path(__file__).resolve().parents[1] / "scripts"
           / "bootstrap.sh").read_text()
    assert "GIT_TERMINAL_PROMPT=0" in src
    assert "CLIQUE_GITHUB_TOKEN" in src
    assert "CLIQUE_SERVER" in src  # server-first source, GitHub is fallback
    assert "fetch_server_bundle" in src
    assert "$'" not in src  # no bashisms: runs under POSIX sh
    # BSD od separates bytes with two spaces, so the check must not
    # rely on single-space "1f 8b" (never matches on macOS tarballs).
    assert 'grep -q "1f 8b"' not in src
    proc = subprocess.run(
        ["sh", "-n", str(Path(__file__).resolve().parents[1]
                         / "scripts" / "bootstrap.sh")],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"bootstrap.sh fails sh -n: {proc.stderr}"


def test_gzip_magic_check_matches_bsd_od(tmp_path):
    """The portable gzip check must match real gzip bytes (regression:
    BSD od prints double spaces, so grep '1f 8b' never matched)."""
    import gzip
    import subprocess
    gz = tmp_path / "x.tgz"
    gz.write_bytes(gzip.compress(b"hello clique"))
    for script in (
        # bootstrap.sh server-bundle + tarball branches
        ["sh", "-c", f"od -An -N2 -tx1 {gz} | tr -d ' \\n' | grep -q '^1f8b'"],
        # join.sh template uses the same pattern (checked textually too)
    ):
        proc = subprocess.run(script, capture_output=True, text=True,
                              timeout=30)
        assert proc.returncode == 0, f"gzip check fails on real gzip: {script}"
    tpl = (Path(__file__).resolve().parents[1] / "scheduler"
           / "server.py").read_text()
    assert 'grep -q "1f 8b"' not in tpl


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


def test_join_forwards_base_url_to_agent(monkeypatch):
    """`clique join --foreground --base-url X ...` reaches agent argv."""
    from typer.testing import CliRunner

    import client.cli as _cli

    seen: dict = {}

    def fake_agent_main():
        import sys as _sys
        seen["argv"] = list(_sys.argv)

    monkeypatch.setattr("node.agent.main", fake_agent_main)
    result = CliRunner().invoke(
        _cli.app,
        ["join", "--server", "http://x:7777", "--runtime", "openai-compat",
         "--model-name", "qwen2.5-coder:7b",
         "--base-url", "http://127.0.0.1:11434/v1",
         "--param-b", "7", "--parallel-slots", "4", "--foreground"],
    )
    assert result.exit_code == 0, result.output
    argv = seen["argv"]
    for flag, val in (("--base-url", "http://127.0.0.1:11434/v1"),
                      ("--model-name", "qwen2.5-coder:7b"),
                      ("--parallel-slots", "4")):
        assert flag in argv and argv[argv.index(flag) + 1] == val


def test_join_direct_call_drops_optioninfo_defaults(monkeypatch):
    """Regression: onboard calls join() directly, so unset options arrive
    as typer OptionInfo defaults. They must not leak into agent argv
    (crashed argparse with 'OptionInfo is not subscriptable')."""
    import node.agent as _agent

    import client.cli as _cli

    seen: dict = {}

    def fake_agent_main():
        import sys as _sys
        seen["argv"] = list(_sys.argv)

    monkeypatch.setattr(_agent, "main", fake_agent_main)
    _cli.join(server="http://x:7777", runtime="echo", param_b=7.0,
              foreground=True)
    argv = seen["argv"]
    assert all(isinstance(x, str) for x in argv), argv
    assert argv == ["clique-agent", "--server", "http://x:7777",
                    "--runtime", "echo", "--param-b", "7.0"]


def test_agent_main_applies_base_url(monkeypatch, tmp_path):
    """`clique-agent --base-url` lands in config (no network touched)."""
    import sys as _sys

    import node.agent as _agent
    from common.config import Config as _Config

    cfg = _Config()
    cfg.node.data_dir = tmp_path / "agent-data"
    monkeypatch.setattr(
        _sys, "argv",
        ["clique-agent", "--base-url", "http://127.0.0.1:8000/v1",
         "--model-name", "nemotron-3-nano-fp8", "--server", "http://x:1"])
    monkeypatch.setattr(_agent, "load", lambda: cfg)
    monkeypatch.setattr(_agent, "NodeAgent", lambda *a, **k: (_ for _ in ()).throw(
        SystemExit("stop-before-network")))
    import pytest as _pt
    with _pt.raises(SystemExit, match="stop-before-network"):
        _agent.main()
    assert cfg.node.openai_base_url == "http://127.0.0.1:8000/v1"
    assert cfg.node.openai_model_name == "nemotron-3-nano-fp8"


async def test_onboard_warns_on_sha_mismatch(headless_server, monkeypatch):
    """Mismatched server_sha warns but still exits 0 with --dry."""
    from typer.testing import CliRunner

    import client.cli as _cli

    monkeypatch.setattr(_cli, "_local_sha", lambda: "deadbee")
    result = await asyncio.to_thread(
        CliRunner().invoke, _cli.app,
        ["onboard", "--server", headless_server, "--runtime", "echo",
         "--dry"],
    )
    assert result.exit_code == 0, result.output
    assert "warn" in result.output
    assert "server ok" in result.output


async def test_onboard_rejects_openai_compat_without_backend(headless_server):
    """Explicit openai-compat with no base-url/model-name exits 2, no join."""
    from typer.testing import CliRunner

    import client.cli as _cli

    result = await asyncio.to_thread(
        CliRunner().invoke, _cli.app,
        ["onboard", "--server", headless_server,
         "--runtime", "openai-compat", "--dry"],
    )
    assert result.exit_code == 2
    assert "needs --base-url" in result.output


async def test_repo_bundle_roundtrips_through_git(headless_server, tmp_path):
    """The served bundle is a real git bundle: `git clone` it."""
    import subprocess
    import urllib.request as _url

    def _do() -> bytes:
        with _url.urlopen(headless_server + "/repo.bundle",
                          timeout=60) as r:
            return r.read()

    blob = await asyncio.to_thread(_do)
    bundle = tmp_path / "tcj.bundle"
    bundle.write_bytes(blob)
    proc = await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "-q", str(bundle), str(tmp_path / "tclone")],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "tclone" / "pyproject.toml").exists()


def test_home_normalize_server():
    from client.home import normalize_server

    assert normalize_server(None) == "http://127.0.0.1:7777"
    assert normalize_server("100.83.233.124:7777") == "http://100.83.233.124:7777"
    assert normalize_server(" http://x:1/ ") == "http://x:1"


async def test_home_app_boots_headless():
    """Home screen boots in pilot mode: 7 buttons, 2 inputs, graceful status."""
    from textual.widgets import Button, Input, Static

    from client.home import HomeApp

    app = HomeApp.build()("http://127.0.0.1:1")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert sorted(b.id for b in app.query(Button)) == [
            "dash", "host", "join", "leave", "refresh", "send", "stop"]
        assert sorted(i.id for i in app.query(Input)) == ["prompt", "server"]
        await app.refresh_all()
        assert "not reachable" in str(app.query_one("#status", Static).content)


async def test_home_join_button_reports_unreachable():
    """Join against nothing listening: button shows error, no crash."""
    from textual.widgets import Static

    from client.home import HomeApp

    app = HomeApp.build()("http://127.0.0.1:1")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._run_join()
        assert "unreachable" in str(app.query_one("#msg", Static).content)
        await app._run_dash()  # flags handoff to dashboard, exits home
        assert app._next == "dash"


def test_ui_command_registered():
    from typer.testing import CliRunner

    from client.cli import app

    result = CliRunner().invoke(app, ["ui", "--help"])
    assert result.exit_code == 0, result.output
    assert "Host / Join" in result.output
