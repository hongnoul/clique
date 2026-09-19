"""Router: allocate each submitted task to the appropriate node.

Policy (implementation priority list, vision dump):
- One task per node at a time.
- Longer prompts go to bigger models.
- Must consider how busy each node is.
- Sessions are sticky to their pinned node; migration is a last resort.

Follows ARCHITECTURE.MD §4: eligibility filter, then estimated
completion scoring, with worker-confirmed reservations and leases.
Heuristic and explainable, not globally optimal.
"""

from __future__ import annotations

from common.types import NodeInfo, TaskAssignment, TaskRequest, TaskResult


class Router:
    def __init__(self, registry: "Registry", db: "Database") -> None: ...

    async def submit(self, request: TaskRequest) -> str:
        """Admit a task into the durable queue.

        Checks idempotency key (same key + equivalent payload = same
        task), queue cap, and basic validity. Returns task_id. Admission
        is persisted before acceptance is reported (§6)."""
        ...

    async def schedule_pending(self) -> list[TaskAssignment]:
        """Main loop step: match queued tasks to READY nodes.

        Step A eligibility: node READY, model compatible with
        request.model_hint or task_type, prompt fits context window,
        node not on battery (unless allowed), telemetry present or
        conservative default applied.

        Step B scoring among eligible nodes:
        - model_fit: longer prompts prefer clusters with larger
          parameter_count_b and context_window.
        - busyness: prefer idle-longest nodes; skip nodes whose recent
          snapshots show pressure (cpu_percent, memory, battery).
        - estimated completion: queue delay + prefill(prompt_len /
          measured_prefill_tok_s) + max_output_tokens /
          measured_decode_tok_s.
        - session affinity: a task with session_id strongly prefers the
          session's pinned node; migration only if pinned node is
          OFFLINE/DRAINING (see sessions.py).

        Emits a reservation per assignment; the node must ACK before the
        lease starts, else revoke and rescore.
        """
        ...

    def score(self, request: TaskRequest, node: NodeInfo) -> tuple[float, str]:
        """Pure scoring function returning (score, human_reason). Kept
        pure/deterministic for unit testing and dashboard explanation."""
        ...

    async def on_result(self, result: TaskResult) -> None:
        """Commit a result atomically: first valid result for a task
        wins; stale/duplicate/lease-expired attempts are rejected
        (LeaseExpiredError) and logged as wasted work. Update the node's
        measured-throughput feedback (§4 Step D)."""
        ...

    async def on_node_lost(self, node_id: str) -> None:
        """Requeue this node's in-flight task with a fresh attempt_id,
        within its retry budget; else fail it."""
        ...

    async def cancel(self, task_id: str, requested_by: str) -> bool:
        """Cancel a task. Documented race winner: a commit that already
        happened beats the cancel (ARCHITECTURE.MD §3)."""
        ...

    async def queue_stats(self) -> dict:
        """Queue depth, per-cluster backlog, wait estimates. Feeds the
        dashboard and the suggestion engine."""
        ...
