"""Unit tests for the session context compiler."""

from __future__ import annotations

import json

from scheduler.context_store import ContextStore, est_tokens, summary_max_output_tokens


def _blob(role: str, content: str, **extra) -> bytes:
    return json.dumps({"role": role, "content": content, **extra}).encode()


def _store(tmp_path) -> tuple[ContextStore, str]:
    store = ContextStore(tmp_path / "ctx.db")
    return store, "s-test"


def test_compile_emits_roles(tmp_path):
    store, sid = _store(tmp_path)
    store.append_turn(sid, 0, _blob("user", "alpha unique token"))
    store.append_turn(sid, 1, _blob("assistant", "ack"))
    store.append_turn(sid, 2, _blob("user", "beta followup"))
    messages = store.compile(sid, context_window=8192, max_output_tokens=1024)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "alpha unique token"
    assert messages[-1]["content"] == "beta followup"


def test_compile_keeps_floor_drops_oldest(tmp_path):
    store, sid = _store(tmp_path)
    # window 400, max_out 64, safety 256 → budget 80 tokens ≈ 320 chars
    old = "OLDTOKEN " + ("x" * 200)
    asst = "ACKTOKEN " + ("y" * 200)
    user = "NEWTOKEN " + ("z" * 80)
    store.append_turn(sid, 0, _blob("user", old))
    store.append_turn(sid, 1, _blob("assistant", asst))
    store.append_turn(sid, 2, _blob("user", user))
    messages = store.compile(sid, context_window=400, max_output_tokens=64)
    roles = [m["role"] for m in messages]
    contents = " ".join(m["content"] for m in messages)
    assert "user" in roles and "assistant" in roles
    assert "NEWTOKEN" in contents
    assert "OLDTOKEN" not in contents
    assert store.needs_compaction(sid, 400, 64)
    overflow = store.overflow_turns(sid, 400, 64)
    assert overflow and overflow[0][1]["content"].startswith("OLDTOKEN")


def test_compile_truncates_current_user_from_front(tmp_path):
    store, sid = _store(tmp_path)
    store.append_turn(sid, 0, _blob("user", "PREFIXDROP " + ("w" * 2000) + " KEEPME"))
    messages = store.compile(sid, context_window=400, max_output_tokens=64)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "KEEPME" in messages[0]["content"]
    assert not messages[0]["content"].startswith("PREFIXDROP")


def test_compaction_covers_prefix(tmp_path):
    store, sid = _store(tmp_path)
    store.append_turn(sid, 0, _blob("user", "ancient " + ("a" * 200)))
    store.append_turn(sid, 1, _blob("assistant", "old ack " + ("b" * 200)))
    store.append_turn(sid, 2, _blob("user", "recent question"))
    store.append_turn(sid, 3, _blob("assistant", "recent answer"))
    plan = store.compaction_plan(sid, context_window=400, max_output_tokens=64)
    assert plan is not None
    assert plan["covers"][0] == 1
    assert plan["messages"][0]["role"] == "system"
    store.append_turn(sid, 4, _blob(
        "system", "folded history", kind="compaction",
        covers_versions=plan["covers"]))
    messages = store.compile(sid, context_window=400, max_output_tokens=64)
    assert messages[0]["role"] == "system"
    assert "folded history" in messages[0]["content"]
    joined = " ".join(m["content"] for m in messages)
    assert "recent question" in joined
    assert "ancient" not in joined
    assert not store.needs_compaction(sid, 400, 64)


def test_est_tokens_matches_request():
    assert est_tokens("abcd") == 1
    assert est_tokens("abcdefgh") == 2


def test_summary_max_output_independent_of_chat_cap():
    # 15% of 8192 = 1228, not the triggering chat's 64
    cap = summary_max_output_tokens(8192, prompt_tokens=100)
    assert cap == 1228
    assert cap != 64
    # clamp so prompt + output still fit
    tight = summary_max_output_tokens(8192, prompt_tokens=7800)
    assert tight == 8192 - 256 - 7800
    assert tight < cap
