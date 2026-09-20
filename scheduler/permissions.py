"""Permission control (the /op system) — implemented.

Minecraft/LuckPerms-inspired (vision dump). Defaults:
- The server node is OWNER.
- The first client node to register is OP (registry handles this).
- Every later node is MEMBER (no op).

Policy modes (ServerConfig.permission_policy):
- "first-client-op": default above.
- "open": every node is treated as OP (trusted home LAN).
- "democracy": far-future; behaves like first-client-op for now.

Persistence: op levels live on NodeInfo inside the registry sqlite (so
they survive rejoin: identity = keypair). This module adds the policy
gate, grant/revoke rules, and a persisted audit log.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from common.errors import PermissionError_
from common.types import OpLevel, utcnow

# action -> minimum level required
_ACTIONS: dict[str, OpLevel] = {
    "approve_cron": OpLevel.OP,
    "kick_node": OpLevel.OP,
    "change_policy": OpLevel.OP,
    "grant_op": OpLevel.OP,
    "manage_vcs": OpLevel.OP,
    "server_access": OpLevel.MEMBER,
    # any node in the clique may stop the server -- not just an op; it's
    # a shared resource and shutdown already confirms before killing
    # anyone's active task
    "shutdown_server": OpLevel.MEMBER,
    "migrate_session": OpLevel.OP,
    "dismiss_suggestion": OpLevel.MEMBER,
}

_RANK = {OpLevel.OWNER: 3, OpLevel.OP: 2, OpLevel.MEMBER: 1, OpLevel.GUEST: 0}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS permission_audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
"""


class PermissionManager:
    def __init__(self, db_path: Path | str, registry, *,
                 policy: str = "first-client-op") -> None:
        self.registry = registry
        self.policy = policy
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._on_change = None  # optional callback(action, actor, target)

    def on_change(self, cb) -> None:
        """Register a callback fired after each audited privileged change
        (used by the server to take vcs snapshots and publish events)."""
        self._on_change = cb

    # -- levels ---------------------------------------------------------------

    def level_of(self, node_id: str) -> OpLevel:
        if self.policy == "open":
            return OpLevel.OP
        info = self.registry.get(node_id)
        if info is None:
            return OpLevel.GUEST
        return info.op_level

    def check(self, node_id: str, action: str) -> None:
        required = _ACTIONS.get(action, OpLevel.OP)
        if _RANK[self.level_of(node_id)] < _RANK[required]:
            raise PermissionError_(
                f"node {node_id[:8]} ({self.level_of(node_id).value}) may not"
                f" {action} (requires {required.value})")

    # -- op grants ------------------------------------------------------------

    def grant_op(self, actor: str, target: str) -> None:
        """`/op <node>`: actor must be OWNER or OP."""
        self.check(actor, "grant_op")
        info = self.registry.get(target)
        if info is None:
            raise PermissionError_(f"unknown target node {target}")
        if info.op_level == OpLevel.OWNER:
            raise PermissionError_("owner level cannot be changed")
        self.registry.set_op_level(target, OpLevel.OP)
        self._audit(actor, "grant_op", target)

    def revoke_op(self, actor: str, target: str) -> None:
        """`/deop <node>`: OWNER can deop anyone; an OP cannot deop the OWNER."""
        self.check(actor, "grant_op")
        info = self.registry.get(target)
        if info is None:
            raise PermissionError_(f"unknown target node {target}")
        if info.op_level == OpLevel.OWNER:
            raise PermissionError_("cannot deop the owner")
        self.registry.set_op_level(target, OpLevel.MEMBER)
        self._audit(actor, "revoke_op", target)

    def set_policy(self, actor: str, policy: str) -> None:
        self.check(actor, "change_policy")
        if policy not in ("first-client-op", "open", "democracy"):
            raise PermissionError_(f"unknown policy {policy!r}")
        self.policy = policy
        self._audit(actor, "change_policy", None, detail=policy)

    # -- audit ----------------------------------------------------------------

    def _audit(self, actor: str, action: str, target: str | None,
               detail: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO permission_audit(at, actor, action, target, detail)"
            " VALUES (?,?,?,?,?)",
            (utcnow().isoformat(), actor, action, target, detail))
        self._db.commit()
        if self._on_change is not None:
            self._on_change(action, actor, target)

    def record(self, actor: str, action: str, target: str | None = None,
               detail: str | None = None) -> None:
        """Audit an already-authorized privileged action (kick, rollback...)."""
        self._audit(actor, action, target, detail)

    def audit_log(self, limit: int = 100) -> list[dict]:
        rows = self._db.execute(
            "SELECT at, actor, action, target, detail FROM permission_audit"
            " ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{"at": at, "actor": actor, "action": action,
                 "target": target, "detail": detail}
                for at, actor, action, target, detail in rows]

    def levels(self) -> list[dict]:
        """Permission table for the dashboard."""
        return [{"node_id": n.node_id, "display_name": n.display_name,
                 "level": (OpLevel.OP if self.policy == "open" and
                           n.op_level == OpLevel.MEMBER else n.op_level).value}
                for n in self.registry.list_nodes()]

    def export_state(self) -> str:
        """Canonical JSON for vcs snapshots."""
        return json.dumps({
            "policy": self.policy,
            "levels": self.levels(),
        }, indent=2, sort_keys=True)
