"""Suggestion engine: detect overloaded task types/nodes and recommend
model changes.

Vision dump example: many tasks are code generation and a node holding
a chat model could support a code-gen model -> show a suggestion on the
dashboard. Suggestions are advisory; the node owner decides
(node/agent.swap_model)."""

from __future__ import annotations

from dataclasses import dataclass
from common.types import TaskType


@dataclass
class Suggestion:
    suggestion_id: str
    kind: str  # "swap_model" | "add_replica" | "add_node"
    target_node: str | None
    from_cluster: str | None
    to_cluster: str | None
    rationale: str  # human-readable, shown in dashboard
    expected_effect: str  # e.g. "code_generation queue wait -50%"
    created_at: "datetime"


class SuggestionEngine:
    def __init__(self, registry: "Registry", router: "Router") -> None: ...

    async def analyze(self) -> list[Suggestion]:
        """Periodic (server.tick) analysis.

        Signals:
        - Per-TaskType queue depth and wait time vs cluster capacity.
        - Idle clusters (nodes READY but rarely assigned).
        - A candidate node's measured resources vs the overloaded
          cluster's model footprint (can it actually fit the model?).

        Emit swap_model when an underused node fits an overloaded
        cluster's model. Hysteresis: do not
        flip-flop suggestions on transient spikes (minimum observation
        window, separate enter/exit thresholds)."""
        ...

    async def active(self) -> list[Suggestion]:
        """Currently valid suggestions for the dashboard."""
        ...

    async def dismiss(self, suggestion_id: str, actor: str) -> None:
        """Owner/op dismisses; suppress identical suggestions for a
        cooldown period."""
        ...

    def overload_report(self) -> dict[TaskType, dict]:
        """Raw per-task-type load stats backing the dashboard heatmap."""
        ...
