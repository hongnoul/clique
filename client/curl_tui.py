"""Stdlib-only live TUI for headless nodes. No pip install required.

Served by the clique server at GET /tui.py so any box with python3
and curl can run a live terminal dashboard immediately:

    export CLIQUE=http://<server-ip>:7777
    curl -fsSL $CLIQUE/tui.py -o /tmp/clique-tui.py
    python3 /tmp/clique-tui.py --server $CLIQUE

One-liner (pipe directly into python3):

    curl -fsSL $CLIQUE/tui.py | python3 - --server $CLIQUE

Snapshot without python (any box with curl):

    curl -s $CLIQUE/dash.txt
    watch -n 2 curl -s $CLIQUE/dash.txt

Only stdlib is used (urllib, json, argparse, curses, time, sys, os)
so this file must never gain third-party imports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_SERVER = os.environ.get("CLIQUE_SERVER", "http://127.0.0.1:7777")
TIMEOUT_S = 5.0


def fetch_json(base: str, path: str):
    url = base.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"_error": f"{path}: {e}"}


def snapshot(base: str) -> dict:
    return {
        "clique": fetch_json(base, "/v1/clique"),
        "nodes": fetch_json(base, "/v1/nodes"),
        "clusters": fetch_json(base, "/v1/clusters"),
        "stats": fetch_json(base, "/v1/stats"),
        "tasks": fetch_json(base, "/v1/tasks"),
    }


def render_lines(snap: dict, width: int = 100) -> list[str]:
    L: list[str] = []
    cl = snap.get("clique") or {}
    if isinstance(cl, dict) and not cl.get("_error"):
        L.append(
            f"clique: {cl.get('name', '?')}  policy={cl.get('policy', '?')}  "
            f"default_model={cl.get('default_model', '?')}"
        )
    else:
        L.append(f"clique: unreachable ({cl})")
    L.append("=" * min(width, 100))

    nodes = snap.get("nodes")
    if isinstance(nodes, list) and nodes:
        L.append(f"NODES ({len(nodes)})  name | status | model | task")
        for n in nodes[:30]:
            name = str(n.get("display_name", "?"))[:16].ljust(16)
            status = str(n.get("status", "?"))[:8].ljust(8)
            model = (n.get("model") or {})
            mkey = f"{model.get('family', '-')}-{model.get('parameter_count_b', '-')}"
            mkey = mkey[:22].ljust(22)
            task = (n.get("current_task_id") or "-")[:8]
            op = str(n.get("op_level", ""))[:6]
            L.append(f"  {name} {status} {mkey} {task} [{op}]")
        if len(nodes) > 30:
            L.append(f"  ... +{len(nodes) - 30} more")
    elif isinstance(nodes, dict) and nodes.get("_error"):
        L.append(f"NODES: {nodes['_error']}")
    else:
        L.append("NODES (0): none joined yet")

    stats = snap.get("stats")
    if isinstance(stats, dict) and not stats.get("_error"):
        by_state = stats.get("by_state", {})
        L.append(f"QUEUE: {by_state}")
    L.append("-" * min(width, 100))

    tasks = snap.get("tasks")
    if isinstance(tasks, list) and tasks:
        L.append(f"TASKS (newest {min(len(tasks), 10)}/{len(tasks)})")
        for t in tasks[:10]:
            req = t.get("request", {}) if isinstance(t, dict) else {}
            prompt = str(req.get("prompt", ""))[:60].replace("\n", " ")
            tid = str(req.get("task_id", "?"))[:12]
            state = str(t.get("state", "?"))[:10].ljust(10)
            node = str(t.get("assigned_node") or "-")[:8]
            L.append(f"  {tid} {state} {node} {prompt}")
    elif isinstance(tasks, dict) and tasks.get("_error"):
        L.append(f"TASKS: {tasks['_error']}")
    else:
        L.append("TASKS: none yet")

    clusters = snap.get("clusters")
    if isinstance(clusters, list) and clusters:
        parts = [f"{c.get('cluster_key')}x{len(c.get('node_ids', []))}" for c in clusters]
        L.append(f"CLUSTERS: {' '.join(parts)}")

    L.append("")
    L.append("[q] quit  [r] refresh now  [s] snapshot-once mode: --once")
    return [ln[:width] for ln in L]


def plain_loop(base: str, interval: float, once: bool) -> int:
    if once:
        print("\n".join(render_lines(snapshot(base))))
        return 0
    try:
        while True:
            snap = snapshot(base)
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(f"clique TUI (plain) -- {base}  refresh {interval}s\n")
            sys.stdout.write("\n".join(render_lines(snap)) + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nbye")
        return 0


def curses_loop(base: str, interval: float) -> int:
    import curses

    def _main(stdscr) -> None:
        stdscr.timeout(int(interval * 1000))
        snap = snapshot(base)  # render immediately, no blank first tick
        while True:
            h, w = stdscr.getmaxyx()
            lines = render_lines(snap, width=max(40, w - 1))
            header = f"clique TUI -- {base}  {time.strftime('%H:%M:%S')}  refresh {interval}s"
            stdscr.erase()
            try:
                stdscr.addnstr(0, 0, header, w - 1)
                for i, ln in enumerate(lines[: h - 1]):
                    stdscr.addnstr(i + 1, 0, ln, w - 1)
            except Exception:
                pass
            stdscr.refresh()
            try:
                key = stdscr.getch()  # blocks up to interval; -1 on tick
            except Exception:
                key = -1
            if key in (ord("q"), ord("Q"), 27):
                return
            snap = snapshot(base)  # one fetch per tick or keypress

    curses.wrapper(_main)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="clique live TUI (stdlib only)")
    p.add_argument("--server", default=DEFAULT_SERVER,
                   help="server URL (or $CLIQUE_SERVER)")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--once", action="store_true",
                   help="print one snapshot and exit (for pipes/cron)")
    p.add_argument("--plain", action="store_true",
                   help="force plain print loop instead of curses")
    args = p.parse_args(argv)

    base = args.server if args.server.startswith("http") else f"http://{args.server}"
    if args.once:
        return plain_loop(base, args.interval, once=True)
    if not args.plain and sys.stdout.isatty() and sys.stdin.isatty():
        try:
            return curses_loop(base, args.interval)
        except Exception as e:
            print(f"curses unavailable ({e}), falling back to plain loop",
                  file=sys.stderr)
    return plain_loop(base, args.interval, once=False)


if __name__ == "__main__":
    raise SystemExit(main())
