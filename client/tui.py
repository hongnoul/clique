"""Textual TUI dashboard: `clique dash`.

Same information architecture as the web dashboard
(client/dashboard/README.md), rendered full-screen in the terminal for
monitor-less nodes accessed over SSH.
"""

from __future__ import annotations


class DashboardApp:
    """textual.app.App subclass (spec).

    Screens: DevicesScreen, SessionsScreen, QueueScreen,
    GovernanceScreen. Bound keys: d/s/q/g to switch, r to refresh,
    o to /op the selected node (op-gated, confirm dialog).
    Data source: CliqueClient.events() stream with initial REST
    snapshots; degrade to 2s polling if the WS drops.
    """

    def __init__(self, client: "CliqueClient") -> None: ...

    async def run_dashboard(self) -> None: ...
