"""Session lifecycle and (rare) cross-node migration within a cluster."""

from __future__ import annotations

from common.types import Session


class SessionManager:
    def __init__(self, db: "Database", context_store: "ContextStore", registry: "Registry") -> None: ...

    async def create(self, owner_node: str, cluster_key: str) -> Session:
        """Open a session pinned to a node the router picks on first
        task. Context lives in the ContextStore from turn one so
        migration is possible later without asking the pinned node."""
        ...

    async def get(self, session_id: str) -> Session: ...

    async def list_active(self) -> list[Session]:
        """Dashboard 'what sessions are being run' view."""
        ...

    async def migrate(self, session_id: str, to_node: str, reason: str) -> Session:
        """Move a session to another node in the same cluster.

        Discouraged path (vision dump: rarely if possible). Preconditions:
        target node READY and in session.cluster_key. Steps: pause
        routing for the session, MsgContextSync the latest context to the
        target, repin, resume. Raises SessionConflictError on concurrent
        migration."""
        ...

    async def watch(self, session_id: str, watcher_node: str) -> None:
        """Subscribe a node/user to live MsgTaskProgress relays for this
        session (future: multiple users viewing the same session)."""
        ...

    async def close(self, session_id: str) -> None: ...
