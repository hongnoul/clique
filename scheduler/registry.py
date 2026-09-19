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
            existing.model = model or existing.model
            existing.resources = resources or existing.resources
            existing.status = NodeStatus.READY if existing.model else NodeStatus.JOINING
            existing.last_heartbeat_at = utcnow()
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

    def mark_offline_stale(self, older_than: timedelta) -> list[NodeInfo]:
        cutoff = utcnow() - older_than
        newly_offline = []
        for info in self.list_nodes():
            if info.status == NodeStatus.OFFLINE or info.role == NodeRole.SERVER:
                continue
            hb = info.last_heartbeat_at
            if hb is not None and hb < cutoff:
                info.status = NodeStatus.OFFLINE
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
        return [n for n in self.list_nodes()
                if n.status == NodeStatus.READY and n.model is not None
                and n.current_task_id is None]

    def remove(self, node_id: str) -> None:
        self.set_status(node_id, NodeStatus.OFFLINE, current_task_id=None)
