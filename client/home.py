"""Seamless home TUI: buttons that call the CLI under the hood.

One screen replaces the multi-line script flow::

    export CLIQUE_SERVER=...   # no more
    curl .../join.sh | sh       # install only
    clique onboard --dry        # no more
    clique onboard              # no more
    clique serve / join / ...   # no more typing

Instead ``clique`` (no args) opens this app::

    [Host]  start a server on this machine   -> cli.serve path
    [Join]  join a server as a node          -> cli.onboard path
    [Chat]  submit a prompt                  -> cli.submit path
    [Dash]  live dashboard                   -> cli.dash --full path
    [Stop]/[Leave]/[Logs]                    -> cli.stop/leave/logs path

All actions reuse the exact same code paths as the CLI (daemon.spawn,
onboard probe + runtime detect, sdk submit) so buttons never drift
from scripts. Pure helpers are unit-testable without a TTY.
"""

from __future__ import annotations

import asyncio
import os
import sys

DEFAULT_SERVER = os.environ.get("CLIQUE_SERVER", "http://127.0.0.1:7777")


# ------------------------------------------------------------- pure helpers


def normalize_server(raw: str | None) -> str:
    """Normalize user input to a URL. Pure, testable."""
    s = (raw or "").strip() or DEFAULT_SERVER
    if not s.startswith("http"):
        s = f"http://{s}"
    return s.rstrip("/")


def probe_server_sync(base: str, timeout_s: float = 4.0) -> dict | None:
    """Probe /v1/clique. Returns info dict or None. Stdlib only, no raise."""
    import json as _json
    import urllib.request as _url

    try:
        with _url.urlopen(base.rstrip("/") + "/v1/clique",
                           timeout=timeout_s) as r:
            data = _json.loads(r.read().decode())
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def local_state(data_dir=None) -> dict:
    """What's running on this machine. Pure-ish (reads pidfiles only)."""
    from common.config import load as load_config

    cfg = load_config() if data_dir is None else None
    dd = data_dir or cfg.node.data_dir
    from client import daemon

    srv = daemon.status(dd, "server")
    agent = daemon.status(dd, "agent")
    return {
        "server_running": srv is not None,
        "server_pid": srv.pid if srv else None,
        "server_addr": (f"{srv.meta.get('host', '?')}:"
                        f"{srv.meta.get('port', '?')}") if srv else None,
        "agent_running": agent is not None,
        "agent_pid": agent.pid if agent else None,
        "agent_server": agent.meta.get("server") if agent else None,
        "agent_name": agent.meta.get("name") if agent else None,
    }


def do_host(port: int | None = None, host: str | None = None) -> str:
    """Start the server in the background. Same path as `clique serve`."""
    from common.config import load as load_config

    config = load_config()
    extra = []
    if port:
        extra += ["--port", str(port)]
    if host:
        extra += ["--host", host]
    from client import daemon

    argv = [sys.executable, "-m", "scheduler.server"] + extra
    bind_host = host or config.server.api_host
    bind_port = port or config.server.api_port
    info = daemon.spawn(config.node.data_dir, "server", argv,
                        meta={"host": bind_host, "port": bind_port})
    return f"server started (pid {info.pid}) on {bind_host}:{bind_port}"


def do_stop_server_local(force: bool = False) -> str:
    """Stop the local server process. Same path as `clique stop --local`."""
    from common.config import load as load_config

    from client import daemon

    config = load_config()
    if not daemon.stop(config.node.data_dir, "server", force=force):
        return "server was not running"
    return "server force-killed" if force else "server stopped"


def do_leave(force: bool = False) -> str:
    """Disconnect the joined node. Same path as `clique leave`."""
    from common.config import load as load_config

    from client import daemon

    config = load_config()
    if not daemon.stop(config.node.data_dir, "agent", force=force):
        return "not joined"
    return "force-quit" if force else "left the clique"


def detect_runtime_sync(base_url: str | None = None,
                        model_name: str | None = None) -> tuple[str, str | None, str | None]:
    """Auto-detect echo vs openai-compat. Same probes as `clique onboard`."""
    import json as _json
    import urllib.request as _url

    def _get(path: str, base: str, timeout: float = 2.0) -> dict | None:
        try:
            with _url.urlopen(base + path, timeout=timeout) as r:
                return _json.loads(r.read().decode())
        except Exception:
            return None

    from client.cli import _detect_runtime

    return _detect_runtime(base_url, model_name, _get)


def do_join(server: str, runtime: str | None = None,
            model_name: str | None = None, base_url: str | None = None,
            param_b: float | None = None,
            parallel_slots: int | None = None) -> str:
    """Join this device to the clique in the background.

    Mirrors `clique onboard` (probe server, warn on SHA drift,
    auto-detect runtime) then `clique join` (daemon.spawn agent).
    Raises RuntimeError with a human message on failure.
    """
    from common.config import load as load_config

    config = load_config()
    srv = normalize_server(server or os.environ.get("CLIQUE_SERVER"))
    info = probe_server_sync(srv)
    if info is None:
        raise RuntimeError(f"server unreachable: {srv} (check VPN/tailscale)")
    from common import protocol as _proto

    notes: list[str] = []
    sproto = info.get("protocol_version")
    if sproto is not None and sproto != _proto.PROTOCOL_VERSION:
        notes.append(f"protocol mismatch (server={sproto} local={_proto.PROTOCOL_VERSION})")
    rt, bu, mn = runtime, base_url, model_name
    if rt in (None, "", "auto"):
        rt, bu, mn = detect_runtime_sync(bu, mn)
    if rt == "openai-compat" and not (bu and mn):
        raise RuntimeError("openai-compat needs a live ollama/llama-server "
                           "or --base-url + --model-name")
    extra = ["--server", srv, "--runtime", rt]
    if mn:
        extra += ["--model-name", mn]
    if bu and rt == "openai-compat":
        extra += ["--base-url", bu]
    if param_b:
        extra += ["--param-b", str(param_b)]
    if parallel_slots:
        extra += ["--parallel-slots", str(parallel_slots)]
    from client import daemon

    argv = [sys.executable, "-m", "node.agent"] + extra
    display_name = config.node.display_name
    try:
        proc = daemon.spawn(config.node.data_dir, "agent", argv,
                            meta={"server": srv, "name": display_name})
    except RuntimeError as e:
        raise RuntimeError(str(e)) from None
    detail = f"joined {srv} (pid {proc.pid}) as {display_name} [{rt}]"
    if notes:
        detail += "  warn: " + "; ".join(notes)
    return detail


async def do_chat(server: str, prompt: str,
                  timeout_s: float = 120.0) -> str:
    """Submit a chat turn and wait. Same path as `clique submit`."""
    from client.sdk import CliqueClient

    client = CliqueClient(normalize_server(server))
    await client.authenticate()
    sid = await client.ensure_chat_session("")
    task_id = await client.submit(prompt, session_id=sid)
    view = await client.wait(task_id, timeout_s=timeout_s)
    if view.result and view.result.output:
        return view.result.output
    err = view.result.error if view.result and view.result.error else view.state.value
    raise RuntimeError(f"{view.state.value}: {err}")


# ------------------------------------------------------------------ TUI app


class HomeApp:
    """Lazy wrapper so `client.home` stays importable without textual."""

    @staticmethod
    def build():
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.widgets import Button, Footer, Header, Input, Static

        _helpers = {
            "normalize_server": normalize_server,
            "probe_server_sync": probe_server_sync,
            "local_state": local_state,
            "do_host": do_host,
            "do_stop_server_local": do_stop_server_local,
            "do_leave": do_leave,
            "do_join": do_join,
        }

        class _Home(App):
            TITLE = "clique"
            BINDINGS = [
                ("h", "host", "Host"),
                ("j", "join", "Join"),
                ("c", "chat_focus", "Chat"),
                ("d", "dash", "Dashboard"),
                ("r", "refresh", "Refresh"),
                ("q", "quit", "Quit"),
            ]

            CSS = (
                "#status { padding: 1 2; border: solid green; height: auto; }"
                "#msg { padding: 1 2; height: auto; }"
                "#answer { padding: 1 2; height: auto; }"
                "Button { margin: 0 1; }"
            )

            def __init__(self, server: str | None = None) -> None:
                super().__init__()
                self.server_url = _helpers["normalize_server"](
                    server or os.environ.get("CLIQUE_SERVER"))
                self._remote: dict | None = None
                self._local: dict = {}
                self._next: str | None = None

            def compose(self) -> ComposeResult:
                yield Header()
                yield Static("starting...", id="status")
                with Vertical():
                    with Horizontal():
                        yield Input(value=self.server_url, id="server",
                                    placeholder="server URL, e.g. http://100.x:7777")
                        yield Button("Refresh", id="refresh", variant="default")
                    with Horizontal():
                        yield Button("Host", id="host", variant="success")
                        yield Button("Stop server", id="stop")
                        yield Button("Join", id="join", variant="primary")
                        yield Button("Leave", id="leave")
                    with Horizontal():
                        yield Input(placeholder="ask the clique... (c to focus)",
                                    id="prompt")
                        yield Button("Send", id="send", variant="primary")
                        yield Button("Dashboard", id="dash")
                    yield Static("", id="answer")
                    yield Static("ready. h host · j join · c chat · d dash · r refresh · q quit",
                                 id="msg")
                yield Footer()

            async def on_mount(self) -> None:
                await self.refresh_all()
                self.set_interval(5.0, self.refresh_all)

            async def refresh_all(self) -> None:
                st = await asyncio.to_thread(_helpers["local_state"])
                remote = await asyncio.to_thread(
                    _helpers["probe_server_sync"], self.server_url)
                self._local, self._remote = st, remote
                try:
                    box = self.query_one("#status", Static)
                except Exception:
                    return
                if st["server_running"]:
                    srv_line = (f"server: RUNNING pid={st['server_pid']} "
                                f"{st['server_addr']}")
                elif remote is not None:
                    srv_line = (f"server: reachable at {self.server_url} "
                                f"({remote.get('name', '?')})")
                else:
                    srv_line = f"server: not reachable ({self.server_url})"
                if st["agent_running"]:
                    node_line = (f"node: JOINED pid={st['agent_pid']} "
                                 f"-> {st['agent_server']}")
                else:
                    node_line = "node: not joined"
                extra = ""
                if isinstance(remote, dict) and remote.get("name"):
                    extra = (f"  clique={remote.get('name')} "
                             f"policy={remote.get('policy', '?')}")
                box.update(f"{srv_line}\n{node_line}{extra}")

            def say(self, text: str) -> None:
                try:
                    self.query_one("#msg", Static).update(text)
                except Exception:
                    pass

            async def action_host(self) -> None:
                await self._run_host()

            async def action_join(self) -> None:
                await self._run_join()

            async def action_chat_focus(self) -> None:
                try:
                    self.query_one("#prompt", Input).focus()
                except Exception:
                    pass

            async def action_dash(self) -> None:
                await self._run_dash()

            async def action_refresh(self) -> None:
                try:
                    self.server_url = _helpers["normalize_server"](
                        self.query_one("#server", Input).value)
                except Exception:
                    pass
                await self.refresh_all()
                self.say("refreshed")

            async def _run_host(self) -> None:
                self.say("starting server...")
                try:
                    msg = await asyncio.to_thread(_helpers["do_host"])
                except RuntimeError as e:
                    self.say(f"already running? {e}")
                    return
                except Exception as e:  # SpawnError carries log tail
                    tail = getattr(e, "log_tail", "")
                    self.say(f"host failed: {e} {tail[-300:]}")
                    return
                self.say(msg)
                await self.refresh_all()

            async def _run_join(self) -> None:
                self.say(f"joining {self.server_url}...")
                try:
                    msg = await asyncio.to_thread(
                        _helpers["do_join"], self.server_url)
                except RuntimeError as e:
                    self.say(str(e))
                    return
                except Exception as e:
                    tail = getattr(e, "log_tail", "")
                    self.say(f"join failed: {e} {tail[-300:]}")
                    return
                self.say(msg)
                await self.refresh_all()

            async def _run_dash(self) -> None:
                # Textual apps cannot nest: quit this app, the run()
                # wrapper below launches the dashboard in this terminal.
                self._next = "dash"
                self.exit()

            async def on_button_pressed(self, event) -> None:
                bid = event.button.id
                if bid == "host":
                    await self._run_host()
                elif bid == "stop":
                    msg = await asyncio.to_thread(
                        _helpers["do_stop_server_local"])
                    self.say(msg)
                    await self.refresh_all()
                elif bid == "join":
                    await self._run_join()
                elif bid == "leave":
                    msg = await asyncio.to_thread(_helpers["do_leave"])
                    self.say(msg)
                    await self.refresh_all()
                elif bid == "refresh":
                    await self.action_refresh()
                elif bid == "dash":
                    await self._run_dash()
                elif bid == "send":
                    await self._run_chat()

            async def _run_chat(self) -> None:
                try:
                    prompt = self.query_one("#prompt", Input).value.strip()
                except Exception:
                    prompt = ""
                if not prompt:
                    self.say("type a prompt first (c to focus)")
                    return
                try:
                    ans = self.query_one("#answer", Static)
                except Exception:
                    ans = None
                if ans is not None:
                    ans.update("thinking...")
                self.say(f"sent to {self.server_url}")
                try:
                    out = await do_chat(self.server_url, prompt)
                except Exception as e:
                    self.say(f"chat failed: {e}")
                    return
                if ans is not None:
                    ans.update(out[:4000])
                self.say("done")

            async def on_input_submitted(self, event) -> None:
                if event.input.id == "server":
                    self.server_url = _helpers["normalize_server"](
                        event.value)
                    await self.refresh_all()
                elif event.input.id == "prompt":
                    await self._run_chat()

        return _Home

    @staticmethod
    def run(server: str | None = None) -> None:
        cls = HomeApp.build()
        app = cls(server)
        app.run()
        if getattr(app, "_next", None) == "dash":
            from client.sdk import CliqueClient
            from client.tui import DashboardApp

            DashboardApp(CliqueClient(app.server_url)).run()


def main(server: str | None = None) -> None:
    """Entry point for `clique` (no args) and `clique ui`."""
    HomeApp.run(server)
