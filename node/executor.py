"""Task executor: accept an assignment, run it locally, report results.

Enforces the node side of the "one task per node" invariant.
"""

from __future__ import annotations

from common.protocol import MsgAssignTask, MsgRevokeTask
from common.types import TaskResult


class TaskExecutor:
    def __init__(self, runtime: "ModelRuntime", server_url: str, session_token: str) -> None: ...

    async def on_assign(self, msg: MsgAssignTask) -> None:
        """Handle an incoming assignment.

        1. Reject (busy) if a task is already running: the router should
           never do this, but the node re-checks (races happen,
           ARCHITECTURE.MD §2).
        2. ACK the reservation within the lease window.
        3. Run runtime.infer_stream, forwarding MsgTaskProgress deltas so
           sessions are watchable live from the dashboard.
        4. Send MsgTaskResult exactly once per attempt; include token
           counts and wall time for router feedback (§4 Step D).
        """
        ...

    async def on_revoke(self, msg: MsgRevokeTask) -> None:
        """Stop generation for the named attempt and confirm. A result
        for a revoked attempt must not be sent afterwards."""
        ...

    def current_result_placeholder(self) -> TaskResult | None:
        """Introspection for the local CLI: what is running right now."""
        ...
