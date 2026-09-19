"""Session lifecycle and (rare) cross-node migration — implemented."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from common.errors import NodeUnavailableError, SessionConflictError
from common.types import NodeStatus, Session, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    session_json TEXT NOT NULL,
    active INTEGER DEFAULT 1
);
"""


class SessionManager:
    def __init__(self, db_path: Path | str, context_store, registry) -> None:
        self.contexts = context_store
        self.registry = registry
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._migrating: set[str] = set()

    def _save(self, session: Session, active: bool = True) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO sessions(session_id, session_json, active)"
            " VALUES (?,?,?)",
            (session.session_id, session.model_dump_json(), int(active)))
        self._db.commit()

    # -- lifecycle ------------------------------------------------------------

    def create(self, owner_node: str, cluster_key: str) -> Session:
        """Open a session. Pinning happens lazily: the router picks a node
        on the first task; context lives in the ContextStore from turn one
        so migration is possible later without asking the pinned node."""
        session = Session(
            session_id=f"s-{uuid.uuid4().hex[:12]}",
            owner_node=owner_node, cluster_key=cluster_key)
        self._save(session)
        return session

    def get(self, session_id: str) -> Session:
        row = self._db.execute(
            "SELECT session_json FROM sessions WHERE session_id=?",
            (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"no such session {session_id}")
        session = Session.model_validate_json(row[0])
        session.context_version = self.contexts.latest_version(session_id)
        return session

    def list_active(self) -> list[Session]:
        """Dashboard 'what sessions are being run' view."""
        rows = self._db.execute(
            "SELECT session_id FROM sessions WHERE active=1").fetchall()
        return [self.get(sid) for (sid,) in rows]

    def pin(self, session_id: str, node_id: str) -> Session:
        """Record the node the router chose for this session's tasks."""
        session = self.get(session_id)
        if session.pinned_node is None:
            session.pinned_node = node_id
            self._save(session)
        return session

    def append_turn(self, session_id: str, expected_version: int,
                    role: str, content: str) -> int:
        self.get(session_id)  # existence check
        blob = json.dumps({"role": role, "content": content,
                           "at": utcnow().isoformat()}).encode()
        return self.contexts.append_turn(session_id, expected_version, blob)

    # -- migration ------------------------------------------------------------

    def migrate(self, session_id: str, to_node: str, reason: str) -> Session:
        """Move a session to another node in the same cluster.

        Discouraged path (vision dump: rarely if possible). Preconditions:
        target node READY and in session.cluster_key. Context already lives
        server-side, so migration is a repin."""
        if session_id in self._migrating:
            raise SessionConflictError(
                f"session {session_id} is already migrating")
        self._migrating.add(session_id)
        try:
            session = self.get(session_id)
            target = self.registry.get(to_node)
            if target is None or target.status not in (
                    NodeStatus.READY, NodeStatus.BUSY):
                raise NodeUnavailableError(f"target node {to_node} unavailable")
            if target.model is None or \
                    target.model.cluster_key() != session.cluster_key:
                raise NodeUnavailableError(
                    f"target node {to_node} not in cluster {session.cluster_key}")
            session.pinned_node = to_node
            self._save(session)
            return session
        finally:
            self._migrating.discard(session_id)

    # -- watching / close -----------------------------------------------------

    def watch(self, session_id: str, watcher_node: str) -> Session:
        """Subscribe a node/user to progress relays for this session."""
        session = self.get(session_id)
        if watcher_node not in session.watchers:
            session.watchers.append(watcher_node)
            self._save(session)
        return session

    def close(self, session_id: str) -> None:
        session = self.get(session_id)
        self._save(session, active=False)

    def export_state(self) -> str:
        """Canonical JSON for vcs snapshots."""
        return json.dumps(
            [s.model_dump(mode="json") for s in self.list_active()],
            indent=2, sort_keys=True)
