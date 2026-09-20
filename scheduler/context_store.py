"""Shared context store on the server node — implemented.

Holds session contexts so any node in a cluster can (rarely) resume a
session. Intra-cluster sharing now; inter-cluster sharing is far future
and would require re-tokenization/summary translation between models.

Storage: sqlite blobs keyed by (session_id, context_version). Turns are
JSON blobs appended with optimistic concurrency.

The worker prompt is compiled from this log: a rolling compaction
message plus a token-budgeted suffix of whole turns, as OpenAI-style
role/content pairs. Original turns are never deleted by compilation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from common.errors import SessionConflictError
from common.types import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_turns (
    session_id TEXT NOT NULL,
    context_version INTEGER NOT NULL,
    turn_blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, context_version)
);
"""

# Compiler budget. Tokens are estimated at 4 chars/token (same as TaskRequest).
SAFETY_TOKENS = 256
SUMMARY_FRACTION = 0.15

COMPACT_INSTRUCTIONS = (
    "Summarize this conversation for a coding assistant. "
    "Preserve file paths, identifiers, unresolved errors, and the current goal. "
    "Drop chit-chat. Do not keep full file dumps."
)


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 1


def summary_max_output_tokens(context_window: int, prompt_tokens: int = 0) -> int:
    """Generation cap for a compaction job.

    Independent of the triggering chat turn's max_output_tokens. Sized as
    a slice of the model window, then clamped so prompt + output still
    fit with SAFETY_TOKENS leftover.
    """
    desired = max(256, int(context_window * SUMMARY_FRACTION))
    room = context_window - SAFETY_TOKENS - max(0, prompt_tokens)
    return min(desired, max(1, room))


def _tail(content: str, max_tokens: int) -> str:
    """Keep the latest characters, estimated at 4 chars/token."""
    max_chars = max(1, max_tokens) * 4
    if len(content) <= max_chars:
        return content
    return content[-max_chars:]


def _as_message(turn: dict, content: str | None = None) -> dict:
    role = turn.get("role", "user")
    if role not in ("system", "user", "assistant"):
        role = "user"
    return {"role": role, "content": turn.get("content", "") if content is None else content}


def join_messages(messages: list[dict]) -> str:
    return "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)


class ContextStore:
    def __init__(self, db_path: Path | str) -> None:
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def latest_version(self, session_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(context_version), 0) FROM context_turns"
            " WHERE session_id=?", (session_id,)).fetchone()
        return row[0]

    def append_turn(self, session_id: str, expected_version: int,
                    turn_blob: bytes) -> int:
        """Append a turn with optimistic concurrency: raises
        SessionConflictError when expected_version is stale. Returns the
        new context_version."""
        current = self.latest_version(session_id)
        if expected_version != current:
            raise SessionConflictError(
                f"session {session_id}: expected version {expected_version},"
                f" current is {current}")
        new_version = current + 1
        self._db.execute(
            "INSERT INTO context_turns(session_id, context_version, turn_blob,"
            " created_at) VALUES (?,?,?,?)",
            (session_id, new_version, turn_blob, utcnow().isoformat()))
        self._db.commit()
        return new_version

    def iter_turns(self, session_id: str) -> list[tuple[int, dict]]:
        rows = self._db.execute(
            "SELECT context_version, turn_blob FROM context_turns"
            " WHERE session_id=? ORDER BY context_version", (session_id,)
        ).fetchall()
        return [(int(v), json.loads(blob)) for v, blob in rows]

    def get_context(self, session_id: str, version: int | None = None) -> bytes:
        """Fetch the full serialized context (latest by default): a JSON
        array of turn objects, the blob shipped in assignments/syncs."""
        cap = version if version is not None else self.latest_version(session_id)
        rows = self._db.execute(
            "SELECT turn_blob FROM context_turns WHERE session_id=?"
            " AND context_version<=? ORDER BY context_version", (session_id, cap)
        ).fetchall()
        turns = [json.loads(b[0]) for b in rows]
        return json.dumps(turns).encode()

    def _budget(self, context_window: int, max_output_tokens: int) -> tuple[int, int]:
        budget = max(1, context_window - max_output_tokens - SAFETY_TOKENS)
        summary_budget = max(1, int(budget * SUMMARY_FRACTION))
        return budget, summary_budget

    def _latest_compaction(self, turns: list[tuple[int, dict]]
                           ) -> tuple[int, dict] | None:
        found: tuple[int, dict] | None = None
        for version, turn in turns:
            if turn.get("kind") == "compaction":
                found = (version, turn)
        return found

    def _covered_through(self, compaction: tuple[int, dict] | None) -> int:
        if compaction is None:
            return 0
        covers = compaction[1].get("covers_versions") or [0, 0]
        if len(covers) < 2:
            return 0
        return int(covers[1])

    def _live_turns(self, turns: list[tuple[int, dict]], covered_through: int
                    ) -> list[tuple[int, dict]]:
        return [
            (v, t) for v, t in turns
            if v > covered_through and t.get("kind") != "compaction"
        ]

    def _fit_floor(self, items: list[tuple[int, dict]], budget: int) -> list[dict]:
        """Keep floor turns in log order. Truncate from the front of the
        newest turn first so the live user request is never dropped."""
        if not items:
            return []
        if len(items) == 1:
            turn = items[0][1]
            content = turn.get("content", "")
            tok = est_tokens(content)
            if tok > budget:
                content = _tail(content, budget)
            return [_as_message(turn, content)]
        older, newer = items[0][1], items[1][1]
        older_c, newer_c = older.get("content", ""), newer.get("content", "")
        older_tok, newer_tok = est_tokens(older_c), est_tokens(newer_c)
        if older_tok + newer_tok <= budget:
            return [_as_message(older), _as_message(newer)]
        if older_tok < budget:
            return [
                _as_message(older),
                _as_message(newer, _tail(newer_c, max(1, budget - older_tok))),
            ]
        older_room = max(1, budget // 2)
        newer_room = max(1, budget - older_room)
        return [
            _as_message(older, _tail(older_c, older_room)),
            _as_message(newer, _tail(newer_c, newer_room)),
        ]

    def compile(self, session_id: str, context_window: int,
                max_output_tokens: int = 1024) -> list[dict]:
        """Windowed messages[] for a worker: optional compaction + recent
        whole turns, fitted to the destination model's context_window."""
        messages, _overflow = self._compile(session_id, context_window,
                                            max_output_tokens)
        return messages

    def _compile(self, session_id: str, context_window: int,
                 max_output_tokens: int
                 ) -> tuple[list[dict], list[tuple[int, dict]]]:
        turns = self.iter_turns(session_id)
        if not turns:
            return [], []
        compaction = self._latest_compaction(turns)
        covered = self._covered_through(compaction)
        live = self._live_turns(turns, covered)
        budget, summary_budget = self._budget(context_window, max_output_tokens)

        prefix: list[dict] = []
        verbatim_budget = budget
        if compaction is not None:
            content = compaction[1].get("content", "")
            stok = est_tokens(content)
            if stok > summary_budget:
                content = _tail(content, summary_budget)
                stok = est_tokens(content)
            prefix = [{"role": "system", "content": content}]
            verbatim_budget = max(1, budget - stok)

        last_user: tuple[int, dict] | None = None
        last_asst: tuple[int, dict] | None = None
        for item in live:
            role = item[1].get("role", "user")
            if role == "user":
                last_user = item
            elif role == "assistant":
                last_asst = item
        floor_items: list[tuple[int, dict]] = [
            item for item in (last_user, last_asst) if item is not None
        ]
        floor_items.sort(key=lambda it: it[0])
        floor_versions = {i[0] for i in floor_items}

        floor_msgs = self._fit_floor(floor_items, verbatim_budget)
        used = sum(est_tokens(m["content"]) for m in floor_msgs)
        leftover = max(0, verbatim_budget - used)

        older = [item for item in live if item[0] not in floor_versions]
        kept_older: list[dict] = []
        kept_older_versions: set[int] = set()
        for version, turn in reversed(older):
            tok = est_tokens(turn.get("content", ""))
            if tok > leftover:
                break
            leftover -= tok
            kept_older.append(_as_message(turn))
            kept_older_versions.add(version)
        kept_older.reverse()

        overflow = [
            item for item in live
            if item[0] not in floor_versions and item[0] not in kept_older_versions
        ]
        return prefix + kept_older + floor_msgs, overflow

    def overflow_turns(self, session_id: str, context_window: int,
                       max_output_tokens: int = 1024) -> list[tuple[int, dict]]:
        _, overflow = self._compile(session_id, context_window, max_output_tokens)
        return overflow

    def needs_compaction(self, session_id: str, context_window: int,
                         max_output_tokens: int = 1024) -> bool:
        return bool(self.overflow_turns(
            session_id, context_window, max_output_tokens))

    def compaction_plan(self, session_id: str, context_window: int,
                        max_output_tokens: int = 1024) -> dict | None:
        """Build the summarizer payload when older turns no longer fit.

        Returns None when the compiled suffix already covers the log.
        ``covers`` is [lo, hi] of context_version, spanning any previous
        compaction plus the overflow about to be folded in.
        """
        turns = self.iter_turns(session_id)
        _, overflow = self._compile(session_id, context_window, max_output_tokens)
        if not overflow:
            return None
        compaction = self._latest_compaction(turns)
        lo = 1 if turns else overflow[0][0]
        if compaction is not None:
            covers = compaction[1].get("covers_versions") or [lo, 0]
            lo = int(covers[0]) if covers else lo
        hi = overflow[-1][0]
        parts: list[str] = []
        if compaction is not None:
            prev = compaction[1].get("content", "").strip()
            if prev:
                parts.append("Previous summary:\n" + prev)
        body = "\n".join(
            f"{t.get('role', 'user')}: {t.get('content', '')}" for _, t in overflow)
        parts.append("Conversation to fold in:\n" + body)
        user_content = "\n\n".join(parts)
        return {
            "covers": [lo, hi],
            "messages": [
                {"role": "system", "content": COMPACT_INSTRUCTIONS},
                {"role": "user", "content": user_content},
            ],
        }

    def truncate_for_model(self, session_id: str, context_window: int,
                           max_output_tokens: int = 1024) -> bytes:
        """Return compiled messages as a JSON blob (fits the window)."""
        return json.dumps(self.compile(
            session_id, context_window, max_output_tokens)).encode()

    def render_prompt(self, session_id: str, context_window: int,
                      max_output_tokens: int = 1024) -> str:
        """Format compiled messages as `role: content` lines.

        Empty sessions yield "". Callers must not append the current user
        turn again; it is already in the store.
        """
        messages = self.compile(session_id, context_window, max_output_tokens)
        if not messages:
            return ""
        return join_messages(messages)

    def delete(self, session_id: str) -> None:
        self._db.execute(
            "DELETE FROM context_turns WHERE session_id=?", (session_id,))
        self._db.commit()

    def gc(self, retain_days: int) -> int:
        """Delete turns older than retain_days. Returns bytes freed."""
        from datetime import timedelta
        cutoff = (utcnow() - timedelta(days=retain_days)).isoformat()
        rows = self._db.execute(
            "SELECT LENGTH(turn_blob) FROM context_turns WHERE created_at<?",
            (cutoff,)).fetchall()
        freed = sum(r[0] for r in rows)
        self._db.execute(
            "DELETE FROM context_turns WHERE created_at<?", (cutoff,))
        self._db.commit()
        return freed
