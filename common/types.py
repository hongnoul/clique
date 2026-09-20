"""Shared domain types for the clique (MVP implementation).

Pydantic models so wire serialization, validation, and API schemas share
one definition. Terminology: node = device, cluster = nodes running the
same model, clique = all nodes; one server node runs the scheduler.
"""

from __future__ import annotations

import enum
import hashlib
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NodeRole(str, enum.Enum):
    SERVER = "server"
    CLIENT = "client"


class OpLevel(str, enum.Enum):
    OWNER = "owner"
    OP = "op"
    MEMBER = "member"
    GUEST = "guest"


class NodeStatus(str, enum.Enum):
    JOINING = "joining"
    READY = "ready"
    BUSY = "busy"
    DRAINING = "draining"
    OFFLINE = "offline"


class TaskState(str, enum.Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class TaskType(str, enum.Enum):
    CHAT = "chat"
    CODE_GENERATION = "code_generation"
    SUMMARIZATION = "summarization"
    EMBEDDING = "embedding"
    OTHER = "other"


class ModelSpec(BaseModel):
    family: str
    parameter_count_b: float
    quantization: str = "none"
    artifact_hash: str | None = None
    context_window: int = 8192
    runtime: str = "echo"  # "echo" | "openai-compat"
    parallel_slots: int = 1  # concurrent tasks this node's runtime can batch (vLLM > 1)
    capabilities: list[TaskType] = Field(default_factory=lambda: [TaskType.CHAT])

    def cluster_key(self) -> str:
        if self.artifact_hash:
            return f"{self.family}:{self.artifact_hash[:12]}"
        return f"{self.family}-{self.parameter_count_b:g}b-{self.quantization}"


class ResourceSnapshot(BaseModel):
    taken_at: datetime = Field(default_factory=utcnow)
    cpu_percent: float | None = None
    memory_total_mb: int | None = None
    memory_available_mb: int | None = None
    gpu_name: str | None = None
    vram_total_mb: int | None = None
    vram_available_mb: int | None = None
    on_battery: bool | None = None
    load_avg_1m: float | None = None
    measured_decode_tok_s: float | None = None
    measured_prefill_tok_s: float | None = None


class NodeInfo(BaseModel):
    node_id: str
    display_name: str
    role: NodeRole = NodeRole.CLIENT
    op_level: OpLevel = OpLevel.MEMBER
    status: NodeStatus = NodeStatus.JOINING
    address: str = ""
    public_key: str = ""  # hex
    model: ModelSpec | None = None
    resources: ResourceSnapshot | None = None
    joined_at: datetime = Field(default_factory=utcnow)
    last_heartbeat_at: datetime | None = None
    current_task_id: str | None = None
    offline_since: datetime | None = None
    last_task_duration_s: float = 0.0  # age of current_task_id when it went offline


class Cluster(BaseModel):
    cluster_key: str
    model: ModelSpec
    node_ids: list[str]


class TaskRequest(BaseModel):
    task_id: str = ""
    submitted_by_node: str = ""
    task_type: TaskType = TaskType.CHAT
    prompt: str
    session_id: str | None = None
    model_hint: str | None = None  # cluster_key prefix; hard filter when set (omit for auto)
    max_output_tokens: int = 1024
    idempotency_key: str
    created_at: datetime = Field(default_factory=utcnow)

    def est_prompt_tokens(self, text: str | None = None) -> int:
        src = self.prompt if text is None else text
        return max(1, len(src) // 4)


class TaskAssignment(BaseModel):
    task_id: str
    attempt_id: str
    node_id: str
    lease_expires_at: datetime
    reason: str


class TaskResult(BaseModel):
    task_id: str
    attempt_id: str
    state: TaskState
    output: str | None = None
    error: str | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    wall_time_s: float | None = None


class TaskView(BaseModel):
    """Full task state as reported by the server API."""

    request: TaskRequest
    state: TaskState
    assigned_node: str | None = None
    attempt_id: str | None = None
    attempts: int = 0
    result: TaskResult | None = None


class Session(BaseModel):
    """Post-MVP: server-held conversation context (see scheduler/sessions.py)."""

    session_id: str
    owner_node: str
    cluster_key: str
    pinned_node: str | None = None
    context_version: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    watchers: list[str] = Field(default_factory=list)


class CronJob(BaseModel):
    """Post-MVP: op-approved scheduled task (see scheduler/cron.py)."""

    cron_id: str
    requested_by: str
    approved_by: str | None = None
    cron_expr: str = ""
    task_template: TaskRequest | None = None
    enabled: bool = False
    last_run_at: datetime | None = None


def node_id_from_public_key(public_key_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()[:16]
