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
TOOL_CALL = "tool_call"  # agent -> server: blocking tool execution request
TOOL_RESULT = "tool_result"  # server -> agent: tool execution outcome
LEAVE = "leave"
SHUTDOWN = "shutdown"
# workspace realtime collab
WS_PATCH = "workspace.patch"      # client -> server: line ops
WS_STATE = "workspace.state"      # server -> client: full file state
WS_DELTA = "workspace.delta"      # server -> clients/agents: applied ops
WS_PRESENCE = "workspace.presence"  # cursor/selection, droppable
WS_SYNC = "workspace.sync"        # client -> server: resync request
WS_INVALIDATE = "workspace.invalidate"  # server -> agent: context stale


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


def msg_tool_call(task_id: str, attempt_id: str, call_id: str,
                  name: str, arguments: dict) -> dict[str, Any]:
    """Agent -> server: execute one tool call, reply with TOOL_RESULT."""
    return {"type": TOOL_CALL, "task_id": task_id, "attempt_id": attempt_id,
            "call_id": call_id, "name": name, "arguments": arguments}


def msg_tool_result(task_id: str, call_id: str, ok: bool,
                    result: Any) -> dict[str, Any]:
    """Server -> agent: outcome of one tool_call (result or error string)."""
    return {"type": TOOL_RESULT, "task_id": task_id, "call_id": call_id,
            "ok": ok, "result": result}


def msg_leave(reason: str) -> dict[str, Any]:
    return {"type": LEAVE, "reason": reason}


def msg_shutdown(reason: str) -> dict[str, Any]:
    return {"type": SHUTDOWN, "reason": reason}
def msg_ws_patch(workspace_id: str, path: str, base_version: int,
                 ops: list[dict], actor: str) -> dict[str, Any]:
    return {"type": WS_PATCH, "workspace_id": workspace_id, "path": path,
            "base_version": base_version, "ops": ops, "actor": actor}


def msg_ws_delta(workspace_id: str, path: str, version: int, seq: int,
                 ops: list[dict], actor: str, rebased: bool = False) -> dict[str, Any]:
    return {"type": WS_DELTA, "workspace_id": workspace_id, "path": path,
            "version": version, "seq": seq, "ops": ops,
            "actor": actor, "rebased": rebased}


def msg_ws_state(workspace_id: str, path: str, version: int, seq: int,
                 text: str) -> dict[str, Any]:
    return {"type": WS_STATE, "workspace_id": workspace_id, "path": path,
            "version": version, "seq": seq, "text": text}


def msg_ws_presence(workspace_id: str, actor: str, path: str | None = None,
                    line: int | None = None) -> dict[str, Any]:
    return {"type": WS_PRESENCE, "workspace_id": workspace_id,
            "actor": actor, "path": path, "line": line}


def msg_ws_sync(workspace_id: str, path: str | None = None,
                since_seq: int = 0) -> dict[str, Any]:
    return {"type": WS_SYNC, "workspace_id": workspace_id,
            "path": path, "since_seq": since_seq}


def msg_ws_invalidate(workspace_id: str, seq: int,
                      paths: list[str]) -> dict[str, Any]:
    return {"type": WS_INVALIDATE, "workspace_id": workspace_id,
            "seq": seq, "paths": paths}


def dumps(msg: dict[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"))


def loads(raw: str | bytes) -> dict[str, Any]:
    msg = json.loads(raw)
    if not isinstance(msg, dict) or "type" not in msg:
        from common.errors import ProtocolError
        raise ProtocolError("message missing type")
    return msg
