"""Wire protocol between node agents and the server node.

Transport: HTTP + WebSocket (FastAPI/uvicorn on the server node,
httpx/websockets on agents). Messages are pydantic-modeled in
implementation; here they are specified as dataclasses plus
encode/decode function contracts.

Security: every message is signed with the node's PyNaCl key.
Registration exchanges public keys; the server issues a session token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from common.types import (
    ModelSpec,
    NodeInfo,
    ResourceSnapshot,
    TaskAssignment,
    TaskRequest,
    TaskResult,
)

PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@dataclass
class Envelope:
    """Outer wrapper for every message."""

    protocol_version: int
    message_type: str  # one of the Msg* class names below
    sender_node_id: str
    sent_at: datetime
    signature: bytes  # detached signature over the canonical payload
    payload: bytes  # canonical JSON of the inner message


def encode(message: object, signing_key: bytes) -> Envelope:
    """Serialize and sign an inner message into an Envelope.

    Raises ProtocolError for unregistered message types.
    """
    ...


def decode(envelope: Envelope, known_keys: dict[str, bytes]) -> object:
    """Verify signature against the sender's registered public key and
    deserialize the inner message.

    Raises SignatureError on verification failure, ProtocolError on
    unknown message_type or version mismatch beyond compatibility window.
    """
    ...


# ---------------------------------------------------------------------------
# Node -> Server messages
# ---------------------------------------------------------------------------


@dataclass
class MsgRegister:
    """First message from a joining node. Server replies MsgRegisterAck.

    The server records join order; the first CLIENT registration is
    granted OP (vision dump governance rule).
    """

    display_name: str
    public_key: bytes
    agent_address: str  # host:port where the node agent listens
    model: ModelSpec | None  # None = wants the default model (Qwen 2.5)
    resources: ResourceSnapshot


@dataclass
class MsgHeartbeat:
    """Periodic status/offer. Doubles as the capacity advertisement.

    Sent every heartbeat_interval_s (config). Missing 3 intervals moves
    the node to OFFLINE in the registry.
    """

    status: str  # NodeStatus value
    resources: ResourceSnapshot
    current_task_id: str | None
    model: ModelSpec | None  # may change if the owner swaps models


@dataclass
class MsgTaskResult:
    """Final (or failed) result for an assigned attempt."""

    result: TaskResult


@dataclass
class MsgTaskProgress:
    """Streaming progress: partial tokens for live session viewing."""

    task_id: str
    attempt_id: str
    token_offset: int
    text_delta: str


@dataclass
class MsgLeave:
    """Graceful departure. Server drains and reroutes queued work."""

    reason: str


# ---------------------------------------------------------------------------
# Server -> Node messages
# ---------------------------------------------------------------------------


@dataclass
class MsgRegisterAck:
    node_id: str
    session_token: str
    op_level: str  # OpLevel value granted at join
    server_public_key: bytes
    default_model: ModelSpec  # what to download if node.model was None


@dataclass
class MsgAssignTask:
    """One task for this node. Node must ACK within ack_timeout_s or the
    router revokes and reassigns (worker-confirmed reservation,
    ARCHITECTURE.MD §2)."""

    assignment: TaskAssignment
    request: TaskRequest
    context_blob: bytes | None  # session context if task joins a session


@dataclass
class MsgRevokeTask:
    """Cancel/timeout an attempt. Node must stop generation and confirm."""

    task_id: str
    attempt_id: str
    reason: str


@dataclass
class MsgContextSync:
    """Push updated session context to a node (rare migration path)."""

    session_id: str
    context_version: int
    context_blob: bytes
