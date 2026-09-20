"""Durable task ledger — accepted-work accounting.

Contribution accounting for a collaborative dev env: one row per
terminal task (succeeded or failed) so dashboards can show per-node
accepted work, failures, and wasted effort. Income generation is a
non-goal; there is no monetary rate.

Storage: sqlite in the server DB. Append-only; no updates.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from common.types import TaskType, TaskView, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    state TEXT NOT NULL,
    node_id TEXT,
    accepted INTEGER NOT NULL,   -- 1 when payout-eligible
    rate REAL NOT NULL,          -- rate credited (0 when not accepted)
    applied_sha TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_node ON ledger(node_id);
CREATE INDEX IF NOT EXISTS idx_ledger_state ON ledger(state);
"""


class Ledger:
    def __init__(self, db_path: Path | str) -> None:
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def record(self, view: TaskView) -> dict:
        """Append one row for a terminal task. Idempotent per task_id."""
        row = self._db.execute(
            "SELECT seq FROM ledger WHERE task_id=?", (view.request.task_id,)
        ).fetchone()
        if row:
            return {"duplicate": True}
        accepted = (
            view.state.value == "succeeded"
            and view.request.task_type == TaskType.CODE_EDIT
            and view.result is not None
            and view.result.applied_sha is not None
        )
        self._db.execute(
            "INSERT INTO ledger(task_id, task_type, state, node_id, accepted,"
            " rate, applied_sha, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (view.request.task_id, view.request.task_type.value,
             view.state.value, view.assigned_node, int(accepted), 0.0,
             view.result.applied_sha if view.result else None,
             utcnow().isoformat()),
        )
        self._db.commit()
        return {"accepted": accepted}

    def summary(self) -> dict:
        rows = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(accepted),0) FROM ledger"
        ).fetchone()
        total, accepted = rows
        by_node = self._db.execute(
            "SELECT node_id, COUNT(*), COALESCE(SUM(accepted),0)"
            " FROM ledger GROUP BY node_id"
        ).fetchall()
        by_state = dict(self._db.execute(
            "SELECT state, COUNT(*) FROM ledger GROUP BY state").fetchall())
        return {
            "total_terminal": total,
            "accepted_tasks": accepted,
            "by_state": by_state,
            "by_node": [
                {"node_id": n or "-", "terminal": c, "accepted": a}
                for n, c, a in by_node],
        }

    def clear(self) -> int:
        """Delete every ledger row. Returns how many were removed."""
        n = self._db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        self._db.execute("DELETE FROM ledger")
        self._db.commit()
        return n

    def export_state(self) -> str:
        import json
        return json.dumps(self.summary(), indent=2, sort_keys=True)
