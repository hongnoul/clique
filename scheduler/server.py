"""Server-node entrypoint: composes registry, router, stores, and API.

Runs on exactly one node in the clique (the hotspot node in the vision
dump). A node agent promotes itself to server when discovery finds none.
"""

from __future__ import annotations

from common.config import Config


class SchedulerServer:
    """Owns all server-side subsystems and their shared sqlite DB."""

    def __init__(self, config: Config) -> None:
        """Wire together: Registry, Router, ContextStore, SessionManager,
        PermissionManager, CronService, VcsService, SuggestionEngine,
        and the FastAPI app from scheduler/api/."""
        ...

    async def start(self) -> None:
        """Open the DB (sqlite WAL), replay durable queue state, take a
        vcs baseline snapshot, start the API server (uvicorn) and the
        mDNS announcement (node/discovery.announce_server)."""
        ...

    async def stop(self) -> None:
        """Withdraw mDNS record, stop accepting jobs, persist state,
        final vcs snapshot, close DB. Nodes fall back to rediscovery."""
        ...

    async def tick(self) -> None:
        """Periodic maintenance: expire leases, mark offline nodes,
        requeue interrupted tasks (within retry budgets), run the
        suggestion engine, fire due cron jobs."""
        ...


def main() -> None:
    """CLI entrypoint ``clique-server`` for running a dedicated server
    node without the auto-promotion path."""
    ...
