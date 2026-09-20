"""Router (MVP implementation).

Policy:
- one task per node at a time
- explicit model_hint is a hard cluster filter (prefix of cluster_key);
  omit it for auto (size / capabilities / busyness)
- sessions stay inside their cluster; prefer the pinned replica if it is
  ready, otherwise hop to any ready node in that cluster
- longer prompts prefer bigger models (parameter_count_b, context fit)
- busyness-aware: skip busy nodes, deprioritize loaded/battery nodes
- explainable scoring: score() is pure and returns a reason string

Durability: task queue persisted in sqlite; leases expire and retry up
to max_attempts; first committed result wins.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from common.errors import LeaseExpiredError
from common.types import (
    NodeInfo,
    TaskAssignment,
    TaskRequest,
    TaskResult,
    TaskState,
    TaskView,
    utcnow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    state TEXT NOT NULL,
    request_json TEXT NOT NULL,
    assigned_node TEXT,
    attempt_id TEXT,
    lease_expires_at TEXT,
    attempts INTEGER DEFAULT 0,
    result_json TEXT,
    created_seq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
"""


class Router:
    def __init__(self, registry, db_path: Path | str, *,
                 lease_seconds: float = 120.0, max_attempts: int = 3,
                 queue_cap: int = 1000, sessions=None, contexts=None,
                 code_lease_seconds: float = 600.0) -> None:
        self.registry = registry
        self.sessions = sessions
        self.contexts = contexts
        self.lease_seconds = lease_seconds
        self.code_lease_seconds = code_lease_seconds
        self.max_attempts = max_attempts
        self.queue_cap = queue_cap
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._seq = 0

    # -- submission -----------------------------------------------------------

    def submit(self, request: TaskRequest) -> str:
        row = self._db.execute(
            "SELECT task_id FROM tasks WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if row:
            return row[0]  # idempotent resubmit
        queued = self._db.execute(
            "SELECT COUNT(*) FROM tasks WHERE state IN ('queued','assigned','running')"
        ).fetchone()[0]
        if queued >= self.queue_cap:
            raise OverflowError("queue full")
        request.task_id = request.task_id or f"t-{uuid.uuid4().hex[:12]}"
        self._seq += 1
        self._db.execute(
            "INSERT INTO tasks(task_id, idempotency_key, state, request_json, created_seq)"
            " VALUES (?,?,?,?,?)",
            (request.task_id, request.idempotency_key, TaskState.QUEUED.value,
             request.model_dump_json(), self._seq),
        )
        self._db.commit()
        return request.task_id

    # -- scoring ---------------------------------------------------------------

    def _placement(self, request: TaskRequest) -> tuple[str | None, str | None]:
        """Return (cluster_prefix, preferred_node). Session cluster wins over hint."""
        cluster = request.model_hint
        preferred = None
        if request.session_id and self.sessions is not None:
            try:
                session = self.sessions.get(request.session_id)
            except KeyError:
                session = None
            if session is not None:
                if session.cluster_key:
                    cluster = session.cluster_key
                preferred = session.pinned_node
        return cluster or None, preferred

    def _worker_prompt(self, request: TaskRequest, node: NodeInfo) -> str:
        """Prompt the node should actually run. Session turns are replayed
        from the server context store, truncated to this model's window."""
        model = node.model
        if (request.session_id and self.contexts is not None
                and model is not None):
            rendered = self.contexts.render_prompt(
                request.session_id, model.context_window)
            if rendered:
                return rendered
        return request.prompt

    @staticmethod
    def score(request: TaskRequest, node: NodeInfo, *,
              prompt: str | None = None,
              preferred_node: str | None = None) -> tuple[float, str]:
        """Pure scoring; higher is better. Returns (score, reason).

        Cluster membership is a hard filter in schedule_pending, not a
        score term. preferred_node is a soft pin (ready replica only).
        """
        model = node.model
        assert model is not None
        reasons = []
        prompt_tokens = request.est_prompt_tokens(prompt)

        # hard-ish fit: prompt must fit context (leave room for output)
        if prompt_tokens + request.max_output_tokens > model.context_window:
            return (-1e9, "prompt exceeds context window")

        score = 0.0
        if preferred_node and node.node_id == preferred_node:
            score += 5000.0
            reasons.append("pinned replica")

        caps = model.capabilities or []
        if request.task_type in caps:
            score += 200.0
            reasons.append(f"capability {request.task_type.value}")
        else:
            score -= 80.0
            reasons.append(f"no {request.task_type.value} capability")

        # longer prompts -> bigger models: weight model size by prompt length
        length_factor = min(prompt_tokens / 1000.0, 4.0)
        score += model.parameter_count_b * length_factor * 10.0
        reasons.append(
            f"size fit {model.parameter_count_b:g}B x len {prompt_tokens}tok")

        # short prompts mildly prefer smaller models (keep big ones free)
        if prompt_tokens < 250:
            score -= model.parameter_count_b
            reasons.append("short prompt: prefer smaller model")

        # busyness: cpu load and battery
        r = node.resources
        if r is not None:
            if r.cpu_percent is not None:
                score -= r.cpu_percent * 0.5
                reasons.append(f"cpu {r.cpu_percent:.0f}%")
            if r.on_battery:
                score -= 25.0
                reasons.append("on battery")
            if r.measured_decode_tok_s:
                score += min(r.measured_decode_tok_s, 100.0) * 0.5
                reasons.append(f"{r.measured_decode_tok_s:.0f} tok/s")
        else:
            score -= 10.0  # conservative when telemetry missing
            reasons.append("no telemetry: conservative")

        return score, "; ".join(reasons)

    # -- scheduling --------------------------------------------------------------

    def schedule_pending(self) -> list[tuple[TaskAssignment, TaskRequest]]:
        """Match queued tasks to nodes with free slots. A node takes up to
        model.parallel_slots concurrent tasks (1 for runtimes that cannot
        batch; >1 for continuous-batching servers like vLLM).

        The TaskRequest on each assignment is a wire copy: session tasks
        have history replayed into ``prompt``. The queued row is unchanged.
        """
        ready = {n.node_id: n for n in self.registry.ready_nodes()}
        # count active assignments per node; drop nodes at slot capacity
        rows = self._db.execute(
            "SELECT assigned_node, COUNT(*) FROM tasks"
            " WHERE state IN ('assigned','running') AND assigned_node IS NOT NULL"
            " GROUP BY assigned_node"
        ).fetchall()
        active = dict(rows)
        for nid in list(ready):
            slots = getattr(ready[nid].model, "parallel_slots", 1) or 1
            if active.get(nid, 0) >= slots:
                del ready[nid]

        out: list[tuple[TaskAssignment, TaskRequest]] = []
        queued = self._db.execute(
            "SELECT request_json FROM tasks WHERE state='queued' ORDER BY created_seq"
        ).fetchall()
        for (req_json,) in queued:
            if not ready:
                break
            request = TaskRequest.model_validate_json(req_json)
            cluster, preferred = self._placement(request)
            candidates = list(ready.values())
            if cluster:
                matched = [
                    n for n in candidates
                    if n.model is not None
                    and n.model.cluster_key().startswith(cluster)
                ]
                if not matched:
                    continue  # wait for this cluster; do not steal another
                candidates = matched
            # pin is preference: if it is not in candidates (busy/offline/
            # wrong cluster), we just score the rest and hop
            scored: list[tuple[tuple[float, str], NodeInfo, str]] = []
            for n in candidates:
                worker_prompt = self._worker_prompt(request, n)
                scored.append((
                    self.score(request, n, prompt=worker_prompt,
                               preferred_node=preferred),
                    n, worker_prompt,
                ))
            scored.sort(key=lambda t: t[0][0], reverse=True)
            (best_score, reason), best, worker_prompt = scored[0]
            if best_score <= -1e9:
                continue  # no eligible node for this task; try next task
            attempt_id = f"a-{uuid.uuid4().hex[:10]}"
            from common.types import TaskType as _TaskType
            lease_s = (self.code_lease_seconds
                       if request.task_type == _TaskType.CODE_EDIT
                       else self.lease_seconds)
            lease = utcnow() + timedelta(seconds=lease_s)
            self._db.execute(
                "UPDATE tasks SET state='assigned', assigned_node=?, attempt_id=?,"
                " lease_expires_at=?, attempts=attempts+1 WHERE task_id=?",
                (best.node_id, attempt_id, lease.isoformat(), request.task_id),
            )
            self._db.commit()
            active[best.node_id] = active.get(best.node_id, 0) + 1
            slots = getattr(best.model, "parallel_slots", 1) or 1
            if active[best.node_id] >= slots:
                del ready[best.node_id]
            wire = request.model_copy()
            wire.prompt = worker_prompt
            out.append((TaskAssignment(
                task_id=request.task_id, attempt_id=attempt_id,
                node_id=best.node_id, lease_expires_at=lease,
                reason=reason,
            ), wire))
        return out

    def mark_running(self, task_id: str, attempt_id: str) -> None:
        self._db.execute(
            "UPDATE tasks SET state='running' WHERE task_id=? AND attempt_id=?"
            " AND state='assigned'", (task_id, attempt_id))
        self._db.commit()

    # -- completion ---------------------------------------------------------------

    def on_result(self, result: TaskResult) -> bool:
        """Atomic commit: only the active attempt in a non-terminal state
        may commit. Returns True if committed."""
        row = self._db.execute(
            "SELECT state, attempt_id, attempts FROM tasks WHERE task_id=?",
            (result.task_id,),
        ).fetchone()
        if row is None:
            return False
        state, attempt_id, attempts = row
        if state in ("succeeded", "failed", "cancelled", "expired"):
            return False  # first result won already
        if attempt_id != result.attempt_id:
            raise LeaseExpiredError(
                f"stale attempt {result.attempt_id} for task {result.task_id}")
        if result.state == TaskState.FAILED and attempts < self.max_attempts:
            # requeue for retry rather than committing failure
            self._db.execute(
                "UPDATE tasks SET state='queued', assigned_node=NULL, attempt_id=NULL,"
                " lease_expires_at=NULL WHERE task_id=?", (result.task_id,))
            self._db.commit()
            return False
        self._db.execute(
            "UPDATE tasks SET state=?, result_json=? WHERE task_id=?",
            (result.state.value, result.model_dump_json(), result.task_id),
        )
        self._db.commit()
        return True

    def task_age_s(self, task_id: str) -> float:
        """Seconds since an in-flight task was assigned, or 0.0 if it isn't
        currently assigned/running (already finished, requeued, or
        unknown). Leases aren't renewed once set, so lease_expires_at minus
        lease_seconds recovers the assignment time without an extra column."""
        row = self._db.execute(
            "SELECT lease_expires_at FROM tasks WHERE task_id=?"
            " AND state IN ('assigned','running')", (task_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return 0.0
        assigned_at = datetime.fromisoformat(row[0]) - timedelta(seconds=self.lease_seconds)
        return max(0.0, (utcnow() - assigned_at).total_seconds())

    def on_node_lost(self, node_id: str) -> list[str]:
        """Requeue (or fail) in-flight tasks of a lost node. Returns requeued ids."""
        rows = self._db.execute(
            "SELECT task_id, attempts FROM tasks WHERE assigned_node=?"
            " AND state IN ('assigned','running')", (node_id,)
        ).fetchall()
        requeued = []
        for task_id, attempts in rows:
            if attempts >= self.max_attempts:
                self._db.execute(
                    "UPDATE tasks SET state='failed', result_json=? WHERE task_id=?",
                    (TaskResult(task_id=task_id, attempt_id="", state=TaskState.FAILED,
                                error="node lost; retry budget exhausted").model_dump_json(),
                     task_id))
            else:
                self._db.execute(
                    "UPDATE tasks SET state='queued', assigned_node=NULL, attempt_id=NULL,"
                    " lease_expires_at=NULL WHERE task_id=?", (task_id,))
                requeued.append(task_id)
        self._db.commit()
        return requeued

    def expire_leases(self) -> list[str]:
        """Requeue tasks whose lease expired (suspected failure)."""
        now = utcnow().isoformat()
        rows = self._db.execute(
            "SELECT task_id, assigned_node FROM tasks WHERE state IN ('assigned','running')"
            " AND lease_expires_at < ?", (now,)
        ).fetchall()
        requeued = []
        for task_id, node_id in rows:
            requeued.extend(self.on_node_lost(node_id))
        return requeued

    def cancel(self, task_id: str) -> bool:
        cur = self._db.execute(
            "UPDATE tasks SET state='cancelled' WHERE task_id=?"
            " AND state IN ('queued','assigned','running')", (task_id,))
        self._db.commit()
        return cur.rowcount > 0

    # -- views ------------------------------------------------------------------

    def get_task(self, task_id: str) -> TaskView | None:
        row = self._db.execute(
            "SELECT request_json, state, assigned_node, attempt_id, attempts, result_json"
            " FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        req_json, state, node, attempt_id, attempts, result_json = row
        return TaskView(
            request=TaskRequest.model_validate_json(req_json),
            state=TaskState(state), assigned_node=node, attempt_id=attempt_id,
            attempts=attempts,
            result=TaskResult.model_validate_json(result_json) if result_json else None,
        )

    def list_tasks(self, state: str | None = None, limit: int = 100) -> list[TaskView]:
        q = "SELECT task_id FROM tasks"
        args: tuple = ()
        if state:
            q += " WHERE state=?"
            args = (state,)
        q += " ORDER BY created_seq DESC LIMIT ?"
        rows = self._db.execute(q, args + (limit,)).fetchall()
        return [v for (tid,) in rows if (v := self.get_task(tid))]

    def queue_stats(self) -> dict:
        rows = self._db.execute(
            "SELECT state, COUNT(*) FROM tasks GROUP BY state").fetchall()
        return {"by_state": dict(rows)}
