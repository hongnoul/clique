"""Wire protocol (MVP implementation).

Transport:
- HTTP POST /v1/register: signed registration (proves keypair ownership).
- WebSocket /ws/agent?token=...: bidirectional JSON messages between a
  node agent and the server.

WS message shape: {"type": <str>, ...fields}. Types below.
"""

from __future__ import annotations

import json
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from common.errors import SignatureError
from common.types import ModelSpec, ResourceSnapshot, TaskAssignment, TaskRequest, TaskResult

PROTOCOL_VERSION = 1

# WS message type constants
HEARTBEAT = "heartbeat"
ASSIGN = "assign"
REVOKE = "revoke"
RESULT = "result"
PROGRESS = "progress"
LEAVE = "leave"


def sign_payload(body: bytes, signing_key_hex: str) -> str:
    """Return hex detached signature over body."""
    sk = SigningKey(bytes.fromhex(signing_key_hex))
    return sk.sign(body).signature.hex()


def verify_payload(body: bytes, signature_hex: str, public_key_hex: str) -> None:
    """Raise SignatureError if signature does not verify."""
    try:
        VerifyKey(bytes.fromhex(public_key_hex)).verify(body, bytes.fromhex(signature_hex))
    except (BadSignatureError, ValueError) as e:
        raise SignatureError(f"registration signature invalid: {e}") from e


# --- WS message builders/parsers -------------------------------------------


def msg_heartbeat(status: str, resources: ResourceSnapshot, current_task_id: str | None,
                  model: ModelSpec | None) -> dict[str, Any]:
    return {
        "type": HEARTBEAT,
        "status": status,
        "resources": resources.model_dump(mode="json"),
        "current_task_id": current_task_id,
        "model": model.model_dump(mode="json") if model else None,
    }


def msg_assign(assignment: TaskAssignment, request: TaskRequest) -> dict[str, Any]:
    return {
        "type": ASSIGN,
        "assignment": assignment.model_dump(mode="json"),
        "request": request.model_dump(mode="json"),
    }


def msg_revoke(task_id: str, attempt_id: str, reason: str) -> dict[str, Any]:
    return {"type": REVOKE, "task_id": task_id, "attempt_id": attempt_id, "reason": reason}


def msg_result(result: TaskResult) -> dict[str, Any]:
    return {"type": RESULT, "result": result.model_dump(mode="json")}


def msg_progress(task_id: str, attempt_id: str, token_offset: int, text_delta: str) -> dict[str, Any]:
    return {"type": PROGRESS, "task_id": task_id, "attempt_id": attempt_id,
            "token_offset": token_offset, "text_delta": text_delta}


def msg_leave(reason: str) -> dict[str, Any]:
    return {"type": LEAVE, "reason": reason}


def dumps(msg: dict[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"))


def loads(raw: str | bytes) -> dict[str, Any]:
    msg = json.loads(raw)
    if not isinstance(msg, dict) or "type" not in msg:
        from common.errors import ProtocolError
        raise ProtocolError("message missing type")
    return msg
