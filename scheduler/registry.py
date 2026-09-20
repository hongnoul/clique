"""Node registry (MVP implementation).

sqlite-backed membership: identity = keypair, join order determines
default op (first client node is OP). Cluster = nodes sharing a
ModelSpec.cluster_key().
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from common.types import (
    Cluster,
    ModelSpec,
    NodeInfo,
    NodeRole,
    NodeStatus,
    OpLevel,
    ResourceSnapshot,
    utcnow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    join_order INTEGER,
    info_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Registry:
    def __init__(self, db_path: Path | str) -> None:
        if isinstance(db_path, Path):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- persistence helpers -------------------------------------------------

    def _save(self, info: NodeInfo, join_order: int | None = None) -> None:
        if join_order is None:
            row = self._db.execute(
                "SELECT join_order FROM nodes WHERE node_id=?", (info.node_id,)
            ).fetchone()
            join_order = row[0] if row else self._next_join_order()
        self._db.execute(
            "INSERT OR REPLACE INTO nodes(node_id, join_order, info_json) VALUES (?,?,?)",
            (info.node_id, join_order, info.model_dump_json()),
        )
        self._db.commit()

    def _next_join_order(self) -> int:
        row = self._db.execute("SELECT COALESCE(MAX(join_order), 0) FROM nodes").fetchone()
        return row[0] + 1

    # -- API ----------------------------------------------------------------

    def register(self, *, node_id: str, display_name: str, public_key: str,
                 address: str, model: ModelSpec | None,
                 resources: ResourceSnapshot | None,
                 role: NodeRole = NodeRole.CLIENT) -> NodeInfo:
        existing = self.get(node_id)
        if existing is not None:
            # rejoin: keep identity, op level, join order
            existing.display_name = display_name
            existing.address = address
            existing.last_heartbeat_at = utcnow()
            if model is not None:
                # a worker re-announcing itself with capacity info -- reset
                # scheduling state, since anything it was doing before
                # going away has already been reassigned by on_node_lost.
                # A model-less call (e.g. the CLI's authenticate(), used
                # just to get a bearer token for op-gated routes) must NOT
                # touch status/current_task_id: it may share this identity
                # with an actually-busy worker (same machine, same
                # keypair), and clobbering that here would both hide a
                # real in-flight task from callers like shutdown's
                # active-task check and wrongly make the scheduler think
                # a busy node is free.
                existing.model = model
                existing.resources = resources or existing.resources
                existing.status = NodeStatus.READY
                existing.current_task_id = None
            self._save(existing)
            return existing

        if role == NodeRole.SERVER:
            op = OpLevel.OWNER
        elif not self._any_client_exists():
            op = OpLevel.OP  # first client node is op
        else:
            op = OpLevel.MEMBER

        info = NodeInfo(
            node_id=node_id, display_name=display_name, role=role, op_level=op,
            status=NodeStatus.READY if model else NodeStatus.JOINING,
            address=address, public_key=public_key, model=model,
            resources=resources, last_heartbeat_at=utcnow(),
        )
        self._save(info, self._next_join_order())
        return info

    def _any_client_exists(self) -> bool:
        for n in self.list_nodes():
            if n.role == NodeRole.CLIENT:
                return True
        return False

    def on_heartbeat(self, node_id: str, *, status: NodeStatus,
                     resources: ResourceSnapshot | None,
                     model: ModelSpec | None,
                     current_task_id: str | None) -> NodeInfo | None:
        info = self.get(node_id)
        if info is None:
            return None
        info.status = status
        info.resources = resources or info.resources
        info.model = model or info.model
        info.current_task_id = current_task_id
        info.last_heartbeat_at = utcnow()
        self._save(info)
        return info

    def set_status(self, node_id: str, status: NodeStatus,
                   current_task_id: str | None = "__keep__") -> None:
        info = self.get(node_id)
        if info is None:
            return
        info.status = status
        if current_task_id != "__keep__":
            info.current_task_id = current_task_id
        self._save(info)

    def mark_offline_stale(self, older_than: timedelta,
                           task_age_fn: Callable[[str], float] | None = None) -> list[NodeInfo]:
        """task_age_fn, if given, is called with a node's current_task_id
        (only when it has one) to snapshot how long that task had been
        running -- must be called before the caller reassigns/clears it
        (e.g. via router.on_node_lost), since that snapshot drives this
        node's reap grace period."""
        cutoff = utcnow() - older_than
        newly_offline = []
        for info in self.list_nodes():
            if info.status == NodeStatus.OFFLINE or info.role == NodeRole.SERVER:
                continue
            hb = info.last_heartbeat_at
            if hb is not None and hb < cutoff:
                info.status = NodeStatus.OFFLINE
                info.offline_since = utcnow()
                info.last_task_duration_s = (
                    task_age_fn(info.current_task_id)
                    if task_age_fn and info.current_task_id else 0.0)
                self._save(info)
                newly_offline.append(info)
        return newly_offline

    def get(self, node_id: str) -> NodeInfo | None:
        row = self._db.execute(
            "SELECT info_json FROM nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        return NodeInfo.model_validate_json(row[0]) if row else None

    def list_nodes(self, status: NodeStatus | None = None) -> list[NodeInfo]:
        rows = self._db.execute(
            "SELECT info_json FROM nodes ORDER BY join_order"
        ).fetchall()
        nodes = [NodeInfo.model_validate_json(r[0]) for r in rows]
        if status is not None:
            nodes = [n for n in nodes if n.status == status]
        return nodes

    def list_clusters(self) -> list[Cluster]:
        by_key: dict[str, Cluster] = {}
        for n in self.list_nodes():
            if n.model is None:
                continue
            key = n.model.cluster_key()
            if key not in by_key:
                by_key[key] = Cluster(cluster_key=key, model=n.model, node_ids=[])
            by_key[key].node_ids.append(n.node_id)
        return list(by_key.values())

    def ready_nodes(self) -> list[NodeInfo]:
        # Nodes eligible for (more) work. Busy nodes stay eligible when their
        # model runtime advertises parallel slots; the router counts active
        # assignments per node and enforces the slot cap.
        out = []
        for n in self.list_nodes():
            if n.model is None:
                continue
            slots = getattr(n.model, "parallel_slots", 1) or 1
            if slots <= 1:
                # legacy single-task nodes: strict READY + idle
                if n.status == NodeStatus.READY and n.current_task_id is None:
                    out.append(n)
            else:
                # batching nodes stay eligible while busy; the router
                # enforces the slot cap from its assignment table
                if n.status in (NodeStatus.READY, NodeStatus.BUSY):
                    out.append(n)
        return out

    def remove(self, node_id: str, task_duration_s: float = 0.0) -> None:
        """Mark a node offline (LEAVE or kick). task_duration_s is how long
        its current task had been running, if any -- feeds the reap grace
        period the same way a stale-heartbeat departure does."""
        info = self.get(node_id)
        if info is None:
            return
        info.status = NodeStatus.OFFLINE
        info.current_task_id = None
        info.offline_since = utcnow()
        info.last_task_duration_s = task_duration_s
        self._save(info)

    def delete(self, node_id: str) -> None:
        """Permanently drop a node's roster entry (past its reap grace
        period). The keypair identity survives, but a later rejoin starts
        a fresh row: new join_order, op_level reset to default."""
        self._db.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
        self._db.commit()

    def set_op_level(self, node_id: str, level: OpLevel) -> NodeInfo | None:
        info = self.get(node_id)
        if info is None:
            return None
        info.op_level = level
        self._save(info)
        return info
