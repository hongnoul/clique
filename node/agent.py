"""Node agent daemon: the process every device runs.

Lifecycle: discover clique -> register -> warm model -> heartbeat loop ->
execute assigned tasks -> drain/leave. If no server node exists on the
network, this node may become the server (see node/discovery.py).

Runs headless (launchd/systemd) so monitor-less devices like the GX10
are first-class; all interaction goes through client/cli.py.
"""

from __future__ import annotations

from common.config import Config
from common.types import NodeRole


class NodeAgent:
    """Long-running daemon for one node."""

    def __init__(self, config: Config) -> None: ...

    async def start(self) -> None:
        """Boot the agent.

        Steps:
        1. Load/create keypair (common.config.generate_or_load_keypair).
        2. Discover an existing server node via mDNS (node/discovery.py).
        3. If none found and config permits, promote self to server
           (spawn scheduler/server.py in-process) and announce.
        4. Register with the server; receive node_id, token, op level,
           and the default model spec if none configured.
        5. Ensure model is downloaded and warm (node/model_runtime.py).
        6. Start heartbeat loop and the task-execution listener.
        """
        ...

    async def stop(self, drain: bool = True) -> None:
        """Shut down.

        With drain=True, finish the current task, send MsgLeave, then
        stop. With drain=False, revoke immediately (task retried
        elsewhere by the router).
        """
        ...

    async def role(self) -> NodeRole:
        """Return whether this agent is currently server or client."""
        ...

    async def swap_model(self, model_ref: str) -> None:
        """Owner-initiated model change (e.g. following a suggestion from
        scheduler/suggestions.py).

        Drains current task, unloads, downloads/loads new model, then
        re-advertises via heartbeat so the registry moves this node to
        the new cluster.
        """
        ...

    async def pause(self) -> None:
        """Owner reclaim: stop accepting tasks, keep model resident."""
        ...

    async def release_resources(self) -> None:
        """Owner reclaim, stronger: unload model weights and KV memory.

        Distinct from pause() per ARCHITECTURE.MD §2 (stopping generation
        vs releasing memory must both be tested).
        """
        ...


def main() -> None:
    """CLI entrypoint: ``clique-agent`` (installed script). Parses config
    path flag, sets up logging, runs NodeAgent under asyncio."""
    ...
