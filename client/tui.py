"""Textual TUI dashboard: `clique dash --full`.

Same information architecture as the web dashboard
(client/dashboard/README.md), rendered full-screen in the terminal for
monitor-less nodes accessed over SSH.

Zero-install alternative (no pip, no clone): the server serves a
stdlib-only live TUI at GET /tui.py; see client/curl_tui.py.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from client.curl_tui import snapshot as fetch_snapshot
from client.sdk import CliqueClient


def summarize(snap: dict) -> dict[str, list[tuple]]:
    """Flatten a snapshot dict into per-tab row tuples (pure, testable)."""
    nodes = snap.get("nodes")
    node_rows: list[tuple] = []
    if isinstance(nodes, list):
        for n in nodes:
            model = n.get("model") or {}
            mkey = f"{model.get('family', '-')}-{model.get('parameter_count_b', '-')}"
            node_rows.append((
                str(n.get("display_name", "?")),
                str(n.get("status", "?")),
                mkey,
                str(n.get("current_task_id") or "-"),
            ))
    tasks = snap.get("tasks")
    task_rows: list[tuple] = []
    if isinstance(tasks, list):
        for t in tasks[:50]:
            req = t.get("request", {}) if isinstance(t, dict) else {}
            task_rows.append((
                str(req.get("task_id", "?"))[:12],
                str(t.get("state", "?")),
                str(t.get("assigned_node") or "-")[:8],
                str(req.get("prompt", ""))[:60].replace("\n", " "),
            ))
    stats = snap.get("stats")
    queue_rows: list[tuple] = []
    if isinstance(stats, dict) and not stats.get("_error"):
        for state, count in sorted((stats.get("by_state") or {}).items()):
            queue_rows.append((state, str(count)))
    clusters = snap.get("clusters")
    gov_rows: list[tuple] = []
    if isinstance(clusters, list):
        for c in clusters:
            gov_rows.append((str(c.get("cluster_key")),
                             str(len(c.get("node_ids", [])))))
    return {"nodes": node_rows, "tasks": task_rows,
            "queue": queue_rows, "clusters": gov_rows}


class DashboardApp(App):
    """textual.app.App subclass.

    Tabs: Devices, Queue, Sessions (tasks), Governance (clusters).
    Keys: d/s/q/g to switch tabs, r to refresh, q to quit.
    Data: REST snapshots every `interval` seconds (polling; the WS
    event stream in scheduler/api/ws.py is still spec-only, so there
    is nothing to degrade from yet).
    """

    BINDINGS = [
        ("d", "tab('devices')", "Devices"),
        ("s", "tab('sessions')", "Sessions"),
        ("q", "tab('queue')", "Queue"),
        ("g", "tab('governance')", "Governance"),
        ("r", "refresh", "Refresh"),
    ]
    TITLE = "clique"

    def __init__(self, client: CliqueClient, interval: float = 2.0) -> None:
        super().__init__()
        self.client = client
        self.base_url = client.base_url
        self.interval = interval
        self._tables: dict[str, DataTable] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="devices"):
            with TabPane("Devices", id="devices"):
                yield self._table("devices", ["name", "status", "model",
                                              "task"])
            with TabPane("Queue", id="queue"):
                yield self._table("queue", ["state", "count"])
            with TabPane("Sessions", id="sessions"):
                yield self._table("sessions", ["task", "state", "node",
                                               "prompt"])
            with TabPane("Governance", id="governance"):
                yield self._table("governance", ["cluster", "nodes"])
        yield Static(f"{self.base_url}  polling every {self.interval}s"
                     "  (d/s/q/g tabs, r refresh)", id="status")
        yield Footer()

    def _table(self, key: str, columns: list[str]) -> DataTable:
        t = DataTable(zebra_stripes=True)
        for col in columns:
            t.add_column(col, key=col)
        self._tables[key] = t
        return t

    async def on_mount(self) -> None:
        await self.refresh_data()
        self.set_interval(self.interval, self.refresh_data)

    async def action_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    async def action_refresh(self) -> None:
        await self.refresh_data()

    async def refresh_data(self) -> None:
        snap = await asyncio.to_thread(fetch_snapshot, self.base_url)
        rows = summarize(snap)
        mapping = {"devices": rows["nodes"], "queue": rows["queue"],
                   "sessions": rows["tasks"],
                   "governance": rows["clusters"]}
        for key, data in mapping.items():
            table = self._tables.get(key)
            if table is None:
                continue
            table.clear()
            for row in data:
                table.add_row(*[str(c) for c in row])
        clique = snap.get("clique") or {}
        try:
            status = self.query_one("#status", Static)
            if isinstance(clique, dict) and not clique.get("_error"):
                status.update(
                    f"{self.base_url}  clique={clique.get('name', '?')}  "
                    f"nodes={len(rows['nodes'])}  tasks={len(rows['tasks'])}"
                    f"  (d/s/q/g tabs, r refresh)")
            else:
                status.update(f"{self.base_url}  unreachable  "
                              "(d/s/q/g tabs, r refresh)")
        except Exception:
            pass

    async def run_dashboard(self) -> None:
        await self.run_async()
