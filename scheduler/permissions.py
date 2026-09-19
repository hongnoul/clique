"""Permission control (the /op system).

Minecraft/LuckPerms-inspired (vision dump). Defaults:
- The server node is OWNER.
- The first client node to register is OP.
- Every later node is MEMBER (no op).

Policy modes (ServerConfig.permission_policy):
- "first-client-op": default above.
- "open": every node is OP (trusted home LAN).
- "democracy": op actions require majority approval of current ops
  (far future; specified for the API shape only).
"""

from __future__ import annotations

from common.types import OpLevel


class PermissionManager:
    def __init__(self, db: "Database") -> None: ...

    async def level_of(self, node_id: str) -> OpLevel: ...

    async def grant_op(self, actor: str, target: str) -> None:
        """`/op <node>`: actor must be OWNER or OP. Persisted, survives
        rejoin (identity = keypair). Raises PermissionError_ otherwise."""
        ...

    async def revoke_op(self, actor: str, target: str) -> None:
        """`/deop <node>`: OWNER can deop anyone; an OP cannot deop the
        OWNER. Raises PermissionError_ on violations."""
        ...

    async def check(self, node_id: str, action: str) -> None:
        """Gate for privileged actions. Action strings: "approve_cron",
        "kick_node", "change_policy", "grant_op", "manage_vcs",
        "server_access". Raises PermissionError_ when not permitted."""
        ...

    async def set_policy(self, actor: str, policy: str) -> None:
        """Change permission_policy at runtime (op-gated, vcs-logged)."""
        ...

    async def audit_log(self, limit: int = 100) -> list[dict]:
        """Recent permission changes and privileged actions, for the
        permission control UI."""
        ...
