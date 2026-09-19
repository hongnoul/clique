"""Shared context store on the server node.

Holds session contexts so any node in a cluster can (rarely) resume a
session. Intra-cluster sharing now; inter-cluster sharing is far future
and would require re-tokenization/summary translation between models.

Storage: sqlite blobs keyed by (session_id, context_version), versioned
snapshots via scheduler/vcs.py.
"""

from __future__ import annotations


class ContextStore:
    def __init__(self, db: "Database") -> None: ...

    async def append_turn(self, session_id: str, expected_version: int, turn_blob: bytes) -> int:
        """Append a turn with optimistic concurrency: raises
        SessionConflictError when expected_version is stale. Returns the
        new context_version."""
        ...

    async def get_context(self, session_id: str, version: int | None = None) -> bytes:
        """Fetch full serialized context (latest by default). This is the
        blob shipped in MsgAssignTask/MsgContextSync."""
        ...

    async def truncate_for_model(self, session_id: str, context_window: int) -> bytes:
        """Return a context blob that fits the target model's window
        (drop/summarize oldest turns). Summarization strategy is a
        routed task itself, far-future."""
        ...

    async def gc(self, retain_days: int) -> int:
        """Delete contexts of expired sessions. Returns bytes freed."""
        ...
