"""Shared domain types for the clique.

Terminology (vision.md): a *node* is one device, a *cluster* is all nodes
running the same model, the *clique* is every node on the network. The
*server node* runs the scheduler; all others are *client nodes*.

Spec only. All bodies are `...`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class NodeRole(enum.Enum):
    """Role a node plays in the clique."""

    SERVER = "server"
    CLIENT = "client"


class OpLevel(enum.Enum):
    """Permission level of a node/user. First client node defaults to OP."""

    OWNER = "owner"  # the server node itself
    OP = "op"  # granted admin (approve cron, /op others, change policy)
    MEMBER = "member"  # normal participant
    GUEST = "guest"  # read-only / rate-limited


class NodeStatus(enum.Enum):
    """Liveness/availability as tracked by the registry."""

    JOINING = "joining"  # registered, model not yet warm
    READY = "ready"  # warm model, idle, accepting tasks
    BUSY = "busy"  # running its one assigned task
    DRAINING = "draining"  # finishing current task, no new assignments
    OFFLINE = "offline"  # missed heartbeats past threshold


class TaskState(enum.Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class TaskType(enum.Enum):
    """Coarse task classes used for routing and overload detection."""

    CHAT = "chat"
    CODE_GENERATION = "code_generation"
    SUMMARIZATION = "summarization"
    EMBEDDING = "embedding"
    OTHER = "other"


@dataclass
class ModelSpec:
    """Identity of a model a node can serve.

    Nodes with equal `family + quantization` (artifact hash when available)
    belong to the same cluster.
    """

    family: str  # e.g. "qwen2.5-coder"
    parameter_count_b: float  # e.g. 7.0
    quantization: str  # e.g. "Q4_K_M"
    artifact_hash: str | None  # GGUF sha256 when known
    context_window: int  # max context tokens
    runtime: str  # "llama-server" | "ollama" | ...
    capabilities: set[TaskType] = field(default_factory=set)

    def cluster_key(self) -> str:
        """Stable key grouping nodes into a cluster (family+size+quant,
        preferring artifact_hash when present)."""
        ...


@dataclass
class ResourceSnapshot:
    """Point-in-time measured resources of a node.

    Missing telemetry MUST be None, never guessed (ARCHITECTURE.MD §2).
    """

    taken_at: datetime
    cpu_percent: float | None
    memory_total_mb: int | None
    memory_available_mb: int | None
    gpu_name: str | None
    vram_total_mb: int | None
    vram_available_mb: int | None
    on_battery: bool | None
    load_avg_1m: float | None
    measured_decode_tok_s: float | None  # from benchmark, not spec sheet
    measured_prefill_tok_s: float | None


@dataclass
class NodeInfo:
    """Registry record for one node."""

    node_id: str  # stable id derived from the node public key
    display_name: str
    role: NodeRole
    op_level: OpLevel
    status: NodeStatus
    address: str  # host:port of the node agent
    public_key: bytes
    model: ModelSpec | None  # None while still choosing/downloading
    resources: ResourceSnapshot | None
    joined_at: datetime
    last_heartbeat_at: datetime | None
    current_task_id: str | None  # one task per node invariant


@dataclass
class Cluster:
    """All nodes serving the same model."""

    cluster_key: str
    model: ModelSpec
    node_ids: list[str]


@dataclass
class TaskRequest:
    """A unit of work submitted from any node."""

    task_id: str
    submitted_by_node: str
    task_type: TaskType
    prompt: str
    session_id: str | None  # attach to a shared context, if any
    model_hint: str | None  # requested cluster_key, or None = router picks
    max_output_tokens: int
    idempotency_key: str
    created_at: datetime


@dataclass
class TaskAssignment:
    """Router decision binding a task to exactly one node."""

    task_id: str
    attempt_id: str  # retries get fresh attempt ids
    node_id: str
    lease_expires_at: datetime
    reason: str  # human-readable routing explanation


@dataclass
class TaskResult:
    task_id: str
    attempt_id: str
    state: TaskState
    output: str | None
    error: str | None
    prompt_tokens: int | None
    output_tokens: int | None
    wall_time_s: float | None


@dataclass
class Session:
    """A conversation/work session whose context lives on the server node.

    Sessions are sticky to one node; migration within a cluster is allowed
    but rare (vision dump: "should be done rarely if possible").
    """

    session_id: str
    owner_node: str
    cluster_key: str
    pinned_node: str | None  # current serving node
    context_version: int  # monotonic, bumps on each appended turn
    created_at: datetime
    watchers: list[str] = field(default_factory=list)  # future: multi-user view


@dataclass
class CronJob:
    """A scheduled task. Requested by any node, runs only after op approval."""

    cron_id: str
    requested_by: str
    approved_by: str | None  # op node id; None = pending
    cron_expr: str  # standard 5-field cron expression
    task_template: TaskRequest
    enabled: bool
    last_run_at: datetime | None
