"""Suggestion engine: detect overloaded task types/nodes and recommend
model changes — implemented.

Vision dump example: many tasks are code generation and a node holding
a chat model could support a code-gen model -> show a suggestion on the
dashboard. Suggestions are advisory; the node owner decides."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from common.types import NodeStatus, TaskState, TaskType, utcnow

# hysteresis (ARCHITECTURE.MD §4 Step D): enter when queue pressure holds
# above ENTER for OBSERVE_WINDOW; a dismissed suggestion is suppressed for
# COOLDOWN so we do not flip-flop on transient spikes.
ENTER_QUEUE_DEPTH = 3
OBSERVE_WINDOW = timedelta(seconds=30)
COOLDOWN = timedelta(minutes=10)


@dataclass
class Suggestion:
    suggestion_id: str
    kind: str  # "swap_model" | "add_replica" | "add_node"
    target_node: str | None
    from_cluster: str | None
    to_cluster: str | None
    rationale: str  # human-readable, shown in dashboard
    expected_effect: str  # e.g. "code_generation queue wait -50%"
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "suggestion_id": self.suggestion_id, "kind": self.kind,
            "target_node": self.target_node, "from_cluster": self.from_cluster,
            "to_cluster": self.to_cluster, "rationale": self.rationale,
            "expected_effect": self.expected_effect,
            "created_at": self.created_at.isoformat(),
        }


class SuggestionEngine:
    def __init__(self, registry, router) -> None:
        self.registry = registry
        self.router = router
        self._active: dict[str, Suggestion] = {}
        # (kind, target, to_cluster) -> suppressed-until
        self._cooldowns: dict[tuple, datetime] = {}
        # cluster_key -> first time overload observed (hysteresis window)
        self._overloaded_since: dict[str, datetime] = {}

    # -- analysis -------------------------------------------------------------

    def _queued_by_hint(self) -> dict[str | None, list]:
        out: dict[str | None, list] = {}
        for view in self.router.list_tasks("queued", limit=500):
            out.setdefault(view.request.model_hint, []).append(view)
        return out

    def analyze(self) -> list[Suggestion]:
        """Periodic (server tick) analysis with hysteresis."""
        now = utcnow()
        clusters = {c.cluster_key: c for c in self.registry.list_clusters()}
        queued = self._queued_by_hint()

        # queue depth per cluster: hinted tasks pile on their cluster;
        # un-hinted tasks spread across all clusters (approximation).
        depth: dict[str, int] = {k: 0 for k in clusters}
        for hint, tasks in queued.items():
            if hint is None:
                continue
            for key in clusters:
                if key.startswith(hint):
                    depth[key] = depth.get(key, 0) + len(tasks)
        unhinted = len(queued.get(None, []))
        if clusters and unhinted:
            for key in clusters:
                depth[key] += unhinted // len(clusters)

        for key, d in depth.items():
            if d >= ENTER_QUEUE_DEPTH:
                self._overloaded_since.setdefault(key, now)
            else:
                self._overloaded_since.pop(key, None)

        fresh: list[Suggestion] = []
        for key, since in self._overloaded_since.items():
            if now - since < OBSERVE_WINDOW:
                continue  # not sustained long enough
            cluster = clusters.get(key)
            if cluster is None:
                continue
            # candidate: an idle node in another cluster whose memory fits
            # the overloaded cluster's model footprint (~1.2 GB per B param
            # at q4-ish quantization; conservative when telemetry missing).
            needed_mb = int(cluster.model.parameter_count_b * 1200)
            for node in self.registry.list_nodes(NodeStatus.READY):
                if node.model is None or node.model.cluster_key() == key:
                    continue
                if node.current_task_id is not None:
                    continue
                mem = (node.resources.memory_available_mb
                       if node.resources else None)
                if mem is not None and mem < needed_mb:
                    continue
                sig = ("swap_model", node.node_id, key)
                if self._cooldowns.get(sig, now) > now:
                    continue
                if any(s.kind == "swap_model" and s.target_node == node.node_id
                       and s.to_cluster == key for s in self._active.values()):
                    continue
                s = Suggestion(
                    suggestion_id=f"sg-{uuid.uuid4().hex[:10]}",
                    kind="swap_model", target_node=node.node_id,
                    from_cluster=node.model.cluster_key(), to_cluster=key,
                    rationale=(
                        f"cluster {key} has {depth[key]} queued task(s) while "
                        f"{node.display_name} sits idle in "
                        f"{node.model.cluster_key()}"),
                    expected_effect=f"{key} queue depth -{depth[key] // 2 or 1}",
                )
                self._active[s.suggestion_id] = s
                fresh.append(s)
                break  # one candidate per overloaded cluster per tick

        # retire suggestions whose overload cleared
        for sid, s in list(self._active.items()):
            if s.to_cluster and s.to_cluster not in self._overloaded_since:
                del self._active[sid]
        return fresh

    # -- views ----------------------------------------------------------------

    def active(self) -> list[Suggestion]:
        """Currently valid suggestions for the dashboard."""
        return list(self._active.values())

    def dismiss(self, suggestion_id: str, actor: str) -> None:
        """Owner/op dismisses; suppress identical suggestions for a
        cooldown period."""
        s = self._active.pop(suggestion_id, None)
        if s is None:
            raise KeyError(f"no such suggestion {suggestion_id}")
        self._cooldowns[(s.kind, s.target_node, s.to_cluster)] = \
            utcnow() + COOLDOWN

    def overload_report(self) -> dict:
        """Raw per-task-type load stats backing the dashboard heatmap."""
        by_type: dict[str, dict] = {t.value: {"queued": 0, "running": 0}
                                    for t in TaskType}
        for view in self.router.list_tasks(limit=500):
            t = view.request.task_type.value
            if view.state == TaskState.QUEUED:
                by_type[t]["queued"] += 1
            elif view.state in (TaskState.ASSIGNED, TaskState.RUNNING):
                by_type[t]["running"] += 1
        return by_type
