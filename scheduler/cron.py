"""Cron: scheduled tasks requested by any node, approved by an op.

APScheduler drives triggers in-process on the server node; croniter
validates/parses expressions. Job definitions live in the server DB
(vcs-snapshotted) so they survive restarts, unlike system crond which
also has no approval hook.
"""

from __future__ import annotations

from common.types import CronJob, TaskRequest


class CronService:
    def __init__(self, db: "Database", router: "Router", permissions: "PermissionManager") -> None: ...

    async def request(self, requested_by: str, cron_expr: str, task_template: TaskRequest) -> CronJob:
        """Any node files a cron request. Validates cron_expr (croniter).
        Job starts pending (approved_by=None) and inert."""
        ...

    async def approve(self, cron_id: str, actor: str) -> CronJob:
        """Op approval (permissions.check(actor, "approve_cron")).
        Registers the APScheduler trigger and enables the job."""
        ...

    async def reject(self, cron_id: str, actor: str, reason: str) -> None: ...

    async def disable(self, cron_id: str, actor: str) -> None:
        """Requester or any op can disable; unregisters the trigger."""
        ...

    async def fire(self, cron_id: str) -> str:
        """Trigger callback: instantiate a TaskRequest from the template
        (fresh task_id/idempotency_key derived from cron_id + scheduled
        time so a missed-then-replayed fire is not duplicated) and submit
        via the router. Returns task_id."""
        ...

    async def list_jobs(self, include_pending: bool = True) -> list[CronJob]: ...
