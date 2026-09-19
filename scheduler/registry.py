"""Node registry: membership, clusters, liveness, join order.

Durable in the server DB so restarts do not forget op grants or join
order (which determines default op: first client node is op).
"""

from __future__ import annotations

from common.protocol import MsgHeartbeat, MsgRegister
from common.types import Cluster, NodeInfo, NodeStatus


class Registry:
    def __init__(self, db: "Database") -> None: ...

    async def register(self, msg: MsgRegister) -> NodeInfo:
        """Admit a node.

        - Derive node_id from the public key; re-registration with the
          same key resumes the old identity (op level survives rejoin).
        - Record join order. If this is the first CLIENT node ever seen,
          grant OpLevel.OP (vision dump rule); all later nodes MEMBER.
        - Place the node in the cluster matching its ModelSpec
          (create the cluster if new).
        """
        ...

    async def on_heartbeat(self, node_id: str, msg: MsgHeartbeat) -> None:
        """Update status/resources/model. A model change moves the node
        between clusters. Refresh last_heartbeat_at."""
        ...

    async def mark_offline_stale(self, now: "datetime") -> list[NodeInfo]:
        """Move nodes past the missed-heartbeat threshold to OFFLINE and
        return them so the router can requeue their tasks."""
        ...

    async def get(self, node_id: str) -> NodeInfo: ...

    async def list_nodes(self, status: NodeStatus | None = None) -> list[NodeInfo]: ...

    async def list_clusters(self) -> list[Cluster]:
        """All clusters with current membership (dashboard 'what devices
        are available' view)."""
        ...

    async def ready_nodes_in_cluster(self, cluster_key: str) -> list[NodeInfo]:
        """READY nodes only: the router's candidate pool."""
        ...

    async def remove(self, node_id: str, reason: str) -> None:
        """Graceful leave (MsgLeave) or op-initiated kick."""
        ...
