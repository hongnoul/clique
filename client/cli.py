"""Headless CLI: the frontend alternative for configuration and the
dashboard (vision dump: needed because the GX10 has no monitor).

Built with typer + rich; `clique dash` opens a textual TUI.

Command tree:

  clique join [--model REF] [--name NAME]     join clique (default model Qwen 2.5)
  clique leave [--now]                        drain (or immediate) departure
  clique status                               this node: role, model, task, resources
  clique nodes                                table of all nodes + clusters
  clique sessions [--watch ID]                list sessions / live-view one
  clique submit PROMPT [--session ID] [--model REF] [--type TYPE]
  clique task TASK_ID [--cancel]
  clique model swap REF                       change this node's model
  clique model suggest                        show active suggestions
  clique op NODE / clique deop NODE           permission control
  clique policy set MODE                      first-client-op | open | democracy
  clique cron add "EXPR" -- PROMPT            request a cron job
  clique cron list / approve ID / reject ID
  clique vcs log / diff A B / rollback SHA
  clique dash                                 full-screen textual dashboard
  clique config edit / show                   manage ~/.clique/config.toml
"""

from __future__ import annotations


def build_app() -> "typer.Typer":
    """Construct the typer app implementing the command tree above.
    Every command resolves a CliqueClient via CliqueClient.discover()
    unless --server URL is given."""
    ...


def render_nodes_table(nodes: list["NodeInfo"]) -> "rich.table.Table":
    """Columns: name, role, op, cluster/model, status, decode tok/s,
    memory, battery, current task."""
    ...


def main() -> None:
    """Entrypoint for the ``clique`` console script."""
    ...
