"""Cron: scheduled tasks requested by any node, approved by an op —
implemented.

croniter validates expressions and computes next-fire times. Firing is
driven by the server tick loop calling ``due()`` (no separate scheduler
process needed; jobs survive restarts because definitions and next-run
times live in the server DB, unlike system crond which also has no
approval hook).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from croniter import croniter

from common.errors import PermissionError_
from common.types import CronJob, TaskRequest, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_jobs (
    cron_id TEXT PRIMARY KEY,
    requested_by TEXT NOT NULL,
    approved_by TEXT,
    cron_expr TEXT NOT NULL,
    template_json TEXT NOT NULL,
    enabled INTEGER DEFAULT 0,
    rejected INTEGER DEFAULT 0,
    next_run_at TEXT,
    last_run_at TEXT
);
"""


class CronService:
    def __init__(self, db_path: Path | str, router, permissions) -> None:
        self.router = router
        self.permissions = permissions
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- requests -------------------------------------------------------------

    def request(self, requested_by: str, cron_expr: str,
                task_template: TaskRequest) -> CronJob:
        """Any node files a cron request. Validates cron_expr. Job starts
        pending (approved_by=None) and inert."""
        if not croniter.is_valid(cron_expr):
            raise ValueError(f"invalid cron expression: {cron_expr!r}")
        cron_id = f"c-{uuid.uuid4().hex[:10]}"
        self._db.execute(
            "INSERT INTO cron_jobs(cron_id, requested_by, cron_expr, template_json)"
            " VALUES (?,?,?,?)",
            (cron_id, requested_by, cron_expr, task_template.model_dump_json()))
        self._db.commit()
        return self.get(cron_id)

    def approve(self, cron_id: str, actor: str) -> CronJob:
        """Op approval: enables the job and arms its next fire time."""
        self.permissions.check(actor, "approve_cron")
        job = self.get(cron_id)
        next_run = croniter(job.cron_expr, utcnow()).get_next(datetime)
        self._db.execute(
            "UPDATE cron_jobs SET approved_by=?, enabled=1, rejected=0,"
            " next_run_at=? WHERE cron_id=?",
            (actor, next_run.isoformat(), cron_id))
        self._db.commit()
        return self.get(cron_id)

    def reject(self, cron_id: str, actor: str, reason: str) -> None:
        self.permissions.check(actor, "approve_cron")
        self.get(cron_id)  # existence check
        self._db.execute(
            "UPDATE cron_jobs SET rejected=1, enabled=0, approved_by=NULL"
            " WHERE cron_id=?", (cron_id,))
        self._db.commit()

    def disable(self, cron_id: str, actor: str) -> None:
        """Requester or any op can disable."""
        job = self.get(cron_id)
        if actor != job.requested_by:
            try:
                self.permissions.check(actor, "approve_cron")
            except PermissionError_:
                raise PermissionError_(
                    f"only the requester or an op may disable {cron_id}")
        self._db.execute(
            "UPDATE cron_jobs SET enabled=0 WHERE cron_id=?", (cron_id,))
        self._db.commit()

    # -- firing ---------------------------------------------------------------

    def due(self, now: datetime | None = None) -> list[str]:
        """Fire all enabled jobs whose next_run_at has passed. Called from
        the server tick loop. Returns fired task_ids."""
        now = now or utcnow()
        rows = self._db.execute(
            "SELECT cron_id FROM cron_jobs WHERE enabled=1 AND next_run_at<=?",
            (now.isoformat(),)).fetchall()
        return [self.fire(cron_id) for (cron_id,) in rows]

    def fire(self, cron_id: str) -> str:
        """Instantiate a TaskRequest from the template with an idempotency
        key derived from cron_id + scheduled time so a missed-then-replayed
        fire is not duplicated. Advances next_run_at. Returns task_id."""
        job = self.get(cron_id)
        row = self._db.execute(
            "SELECT template_json, next_run_at FROM cron_jobs WHERE cron_id=?",
            (cron_id,)).fetchone()
        template = TaskRequest.model_validate_json(row[0])
        scheduled = row[1] or utcnow().isoformat()
        request = template.model_copy(update={
            "task_id": "",
            "idempotency_key": hashlib.sha256(
                f"{cron_id}:{scheduled}".encode()).hexdigest()[:32],
            "created_at": utcnow(),
        })
        task_id = self.router.submit(request)
        next_run = croniter(job.cron_expr, utcnow()).get_next(datetime)
        self._db.execute(
            "UPDATE cron_jobs SET last_run_at=?, next_run_at=? WHERE cron_id=?",
            (utcnow().isoformat(), next_run.isoformat(), cron_id))
        self._db.commit()
        return task_id

    # -- views ----------------------------------------------------------------

    def get(self, cron_id: str) -> CronJob:
        row = self._db.execute(
            "SELECT cron_id, requested_by, approved_by, cron_expr,"
            " template_json, enabled, last_run_at FROM cron_jobs WHERE cron_id=?",
            (cron_id,)).fetchone()
        if row is None:
            raise KeyError(f"no such cron job {cron_id}")
        return CronJob(
            cron_id=row[0], requested_by=row[1], approved_by=row[2],
            cron_expr=row[3], task_template=TaskRequest.model_validate_json(row[4]),
            enabled=bool(row[5]),
            last_run_at=datetime.fromisoformat(row[6]) if row[6] else None)

    def list_jobs(self, include_pending: bool = True) -> list[CronJob]:
        q = "SELECT cron_id FROM cron_jobs"
        if not include_pending:
            q += " WHERE approved_by IS NOT NULL"
        return [self.get(cid) for (cid,) in self._db.execute(q).fetchall()]

    def export_state(self) -> str:
        """Canonical JSON for vcs snapshots."""
        return json.dumps(
            [j.model_dump(mode="json") for j in self.list_jobs()],
            indent=2, sort_keys=True)
