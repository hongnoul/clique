"""Shared context store on the server node — implemented.

Holds session contexts so any node in a cluster can (rarely) resume a
session. Intra-cluster sharing now; inter-cluster sharing is far future
and would require re-tokenization/summary translation between models.

Storage: sqlite blobs keyed by (session_id, context_version). Turns are
JSON blobs appended with optimistic concurrency.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from common.errors import SessionConflictError
from common.types import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_turns (
    session_id TEXT NOT NULL,
    context_version INTEGER NOT NULL,
    turn_blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, context_version)
);
"""


class ContextStore:
    def __init__(self, db_path: Path | str) -> None:
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def latest_version(self, session_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(context_version), 0) FROM context_turns"
            " WHERE session_id=?", (session_id,)).fetchone()
        return row[0]

    def append_turn(self, session_id: str, expected_version: int,
                    turn_blob: bytes) -> int:
        """Append a turn with optimistic concurrency: raises
        SessionConflictError when expected_version is stale. Returns the
        new context_version."""
        current = self.latest_version(session_id)
        if expected_version != current:
            raise SessionConflictError(
                f"session {session_id}: expected version {expected_version},"
                f" current is {current}")
        new_version = current + 1
        self._db.execute(
            "INSERT INTO context_turns(session_id, context_version, turn_blob,"
            " created_at) VALUES (?,?,?,?)",
            (session_id, new_version, turn_blob, utcnow().isoformat()))
        self._db.commit()
        return new_version

    def get_context(self, session_id: str, version: int | None = None) -> bytes:
        """Fetch the full serialized context (latest by default): a JSON
        array of turn objects, the blob shipped in assignments/syncs."""
        cap = version if version is not None else self.latest_version(session_id)
        rows = self._db.execute(
            "SELECT turn_blob FROM context_turns WHERE session_id=?"
            " AND context_version<=? ORDER BY context_version", (session_id, cap)
        ).fetchall()
        turns = [json.loads(b[0]) for b in rows]
        return json.dumps(turns).encode()

    def truncate_for_model(self, session_id: str, context_window: int) -> bytes:
        """Return a context blob that fits the target model's window by
        dropping oldest turns (est. 4 chars/token). Summarization is a
        routed task itself, far-future."""
        turns = json.loads(self.get_context(session_id))
        budget = context_window * 4  # chars
        kept: list = []
        used = 0
        for turn in reversed(turns):
            size = len(json.dumps(turn))
            if used + size > budget and kept:
                break
            kept.append(turn)
            used += size
        kept.reverse()
        return json.dumps(kept).encode()

    def delete(self, session_id: str) -> None:
        self._db.execute(
            "DELETE FROM context_turns WHERE session_id=?", (session_id,))
        self._db.commit()

    def gc(self, retain_days: int) -> int:
        """Delete turns older than retain_days. Returns bytes freed."""
        from datetime import timedelta
        cutoff = (utcnow() - timedelta(days=retain_days)).isoformat()
        rows = self._db.execute(
            "SELECT LENGTH(turn_blob) FROM context_turns WHERE created_at<?",
            (cutoff,)).fetchall()
        freed = sum(r[0] for r in rows)
        self._db.execute(
            "DELETE FROM context_turns WHERE created_at<?", (cutoff,))
        self._db.commit()
        return freed
