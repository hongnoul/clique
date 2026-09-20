"""Unit tests for cluster-local session routing, context replay, and
specify-vs-auto scoring. No live agents.
"""

from __future__ import annotations

import asyncio

import pytest

from common.config import Config
from common.errors import SessionConflictError
from common.types import (
    ChatMessage,
    ModelSpec,
    NodeStatus,
    ResourceSnapshot,
    TaskRequest,
    TaskState,
    TaskType,
)
from node.model_runtime import capabilities_for_family
from scheduler.context_store import ContextStore
from scheduler.registry import Registry
from scheduler.router import Router
from scheduler.sessions import SessionManager


def _pool(tmp_path, *nodes: tuple[str, ModelSpec]):
    db = tmp_path / "unit.db"
    registry = Registry(db)
    res = ResourceSnapshot(memory_available_mb=16_000)
    for node_id, model in nodes:
        registry.register(
            node_id=node_id, display_name=node_id, public_key="",
            address="", model=model, resources=res)
    contexts = ContextStore(db)
    sessions = SessionManager(db, contexts, registry)
    router = Router(registry, db, sessions=sessions, contexts=contexts)
    return registry, router, sessions, contexts


def _spec(b: float, family: str = "echo", **kw) -> ModelSpec:
    return ModelSpec(family=family, parameter_count_b=b, **kw)


def test_capabilities_from_family():
    assert TaskType.CHAT in capabilities_for_family("echo")
    assert TaskType.CODE_GENERATION in capabilities_for_family("qwen2.5-coder")
    assert capabilities_for_family("nomic-embed") == [TaskType.EMBEDDING]


def test_explicit_model_hint_is_hard_cluster_filter(tmp_path):
    seven = _spec(7.0)
    seventy = _spec(70.0)
    _, router, _, _ = _pool(
        tmp_path, ("n-small", seven), ("n-big", seventy))
    tid = router.submit(TaskRequest(
        prompt="hello", model_hint="echo-7b-none",
        idempotency_key="h1"))
    assigned, _ = router.schedule_pending()[0]
    assert assigned.node_id == "n-small"
    view = router.get_task(tid)
    assert view.assigned_node == "n-small"


def test_hinted_task_waits_when_cluster_busy(tmp_path):
    seven = _spec(7.0)
    seventy = _spec(70.0)
    registry, router, _, _ = _pool(
        tmp_path, ("n-small", seven), ("n-big", seventy))
    registry.set_status("n-small", NodeStatus.READY, current_task_id="hold")
    tid = router.submit(TaskRequest(
        prompt="only 7b please", model_hint="echo-7b-none",
        idempotency_key="h2"))
    assert router.schedule_pending() == []
    assert router.get_task(tid).state == TaskState.QUEUED


def test_auto_routes_code_to_capable_node(tmp_path):
    chat = _spec(7.0, capabilities=[TaskType.CHAT])
    coder = _spec(7.0, family="echo-coder",
                  capabilities=[TaskType.CHAT, TaskType.CODE_GENERATION])
    _, router, _, _ = _pool(tmp_path, ("n-chat", chat), ("n-coder", coder))
    router.submit(TaskRequest(
        prompt="implement fizzbuzz", task_type=TaskType.CODE_GENERATION,
        idempotency_key="code"))
    assigned, _ = router.schedule_pending()[0]
    assert assigned.node_id == "n-coder"
    assert "capability code_generation" in assigned.reason


def test_session_prefers_ready_pin(tmp_path):
    seven = _spec(7.0)
    _, router, sessions, _ = _pool(
        tmp_path, ("n1", seven), ("n2", seven))
    sess = sessions.create("owner", "echo-7b-none")
    sessions.pin(sess.session_id, "n2")
    router.submit(TaskRequest(
        prompt="turn", session_id=sess.session_id,
        model_hint="echo-7b-none", idempotency_key="pin"))
    assigned, _ = router.schedule_pending()[0]
    assert assigned.node_id == "n2"
    assert "pinned replica" in assigned.reason


def test_session_hops_when_pin_busy(tmp_path):
    seven = _spec(7.0)
    registry, router, sessions, _ = _pool(
        tmp_path, ("n1", seven), ("n2", seven))
    sess = sessions.create("owner", "echo-7b-none")
    sessions.pin(sess.session_id, "n1")
    registry.set_status("n1", NodeStatus.READY, current_task_id="other")
    router.submit(TaskRequest(
        prompt="hop please", session_id=sess.session_id,
        model_hint="echo-7b-none", idempotency_key="hop"))
    assigned, _ = router.schedule_pending()[0]
    assert assigned.node_id == "n2"


def test_session_replays_truncated_history(tmp_path):
    seven = _spec(7.0)
    _, router, sessions, _ = _pool(tmp_path, ("n1", seven))
    sess = sessions.create("owner", "echo-7b-none")
    sessions.append_turn(sess.session_id, 0, "user", "alpha unique token")
    sessions.append_turn(sess.session_id, 1, "assistant", "ack")
    sessions.append_turn(sess.session_id, 2, "user", "beta followup")
    router.submit(TaskRequest(
        prompt="beta followup", session_id=sess.session_id,
        model_hint="echo-7b-none", idempotency_key="ctx"))
    _, wire = router.schedule_pending()[0]
    assert wire.messages is not None
    assert [m.role for m in wire.messages] == ["user", "assistant", "user"]
    assert wire.messages[0].content == "alpha unique token"
    assert wire.messages[-1].content == "beta followup"
    assert wire.prompt.startswith("user: alpha unique token")
    assert wire.prompt.endswith("user: beta followup")
    # queued row keeps the original user turn, not the expanded prompt
    view = router.get_task(wire.task_id)
    assert view.request.prompt == "beta followup"
    assert view.request.messages is None


def test_bind_cluster_only_when_unbound(tmp_path):
    seven = _spec(7.0)
    _, _, sessions, _ = _pool(tmp_path, ("n1", seven))
    sess = sessions.create("owner", "")
    sessions.bind_cluster(sess.session_id, "echo-7b-none")
    assert sessions.get(sess.session_id).cluster_key == "echo-7b-none"
    sessions.bind_cluster(sess.session_id, "echo-70b-none")
    assert sessions.get(sess.session_id).cluster_key == "echo-7b-none"


def test_compaction_job_does_not_replay_session(tmp_path):
    seven = _spec(7.0)
    _, router, sessions, _ = _pool(tmp_path, ("n1", seven))
    sess = sessions.create("owner", "echo-7b-none")
    sessions.append_turn(sess.session_id, 0, "user", "secret history token")
    router.submit(TaskRequest(
        prompt="fold me",
        messages=[
            ChatMessage(role="system", content="Summarize"),
            ChatMessage(role="user", content="fold me"),
        ],
        session_id=sess.session_id,
        record_turns=False,
        task_type=TaskType.SUMMARIZATION,
        compaction_covers=[1, 1],
        idempotency_key="compact-job",
    ))
    assigned, wire = router.schedule_pending()[0]
    assert "chat (summarize)" in assigned.reason
    assert [m.role for m in wire.messages] == ["system", "user"]
    assert wire.messages[0].content == "Summarize"
    assert "secret history token" not in wire.prompt


def test_append_turn_latest_retries_stale_version(tmp_path):
    _, _, sessions, _ = _pool(tmp_path, ("n1", _spec(7.0)))
    sess = sessions.create("owner", "echo-7b-none")
    sessions.append_turn(sess.session_id, 0, "user", "a")
    with pytest.raises(SessionConflictError):
        sessions.append_turn(sess.session_id, 0, "user", "stale")
    n = sessions.append_turn_latest(sess.session_id, "user", "b")
    assert n == 2
    assert sessions.contexts.latest_version(sess.session_id) == 2


def test_compaction_does_not_relay_session_stream():
    chat = TaskRequest(prompt="hi", session_id="s-1", idempotency_key="a")
    assert chat.relays_session_stream()
    job = TaskRequest(
        prompt="sum", session_id="s-1", record_turns=False,
        task_type=TaskType.SUMMARIZATION, idempotency_key="b")
    assert not job.relays_session_stream()
    oneshot = TaskRequest(prompt="hi", idempotency_key="c")
    assert not oneshot.relays_session_stream()


def test_maybe_compact_uses_window_output_cap(tmp_path):
    from scheduler.context_store import summary_max_output_tokens
    from scheduler.server import SchedulerServer

    cfg = Config()
    cfg.node.data_dir = tmp_path / "n"
    cfg.server.db_path = tmp_path / "s.db"
    server = SchedulerServer(cfg)
    sess = server.sessions.create("owner", "echo-7b-none")
    sid = sess.session_id
    pad = "unique words " * 500
    for i in range(8):
        server.sessions.append_turn_latest(sid, "user", f"turn {i} {pad}")
        server.sessions.append_turn_latest(sid, "assistant", f"ack {i}")
    asyncio.run(server._maybe_compact(sid, max_output_tokens=64))
    sums = [v for v in server.router.list_tasks()
            if v.request.task_type == TaskType.SUMMARIZATION]
    assert len(sums) == 1
    req = sums[0].request
    assert req.max_output_tokens != 64
    assert req.max_output_tokens == summary_max_output_tokens(
        8192, req.est_prompt_tokens())
    assert req.record_turns is False


def test_clear_wipes_sessions_queue_and_data(tmp_path):
    from common.types import CodeTaskSpec, TaskResult, TaskView
    from scheduler.server import SchedulerServer

    cfg = Config()
    cfg.node.data_dir = tmp_path / "n"
    cfg.server.db_path = tmp_path / "s.db"
    server = SchedulerServer(cfg)

    sess = server.sessions.create("owner", "echo-7b-none")
    sid = sess.session_id
    server.sessions.append_turn_latest(sid, "user", "hello")
    server.sessions.append_turn_latest(sid, "assistant", "ack")
    tid = server.router.submit(TaskRequest(
        prompt="queued", idempotency_key="k-clear"))
    server.progress[tid] = "partial"
    server.race_groups["r1"] = [tid]
    server._compacting.add(sid)
    server.live_workspaces.create("w-clear", {"a.py": "print(1)\n"})
    server.workspaces.create("t-ws", CodeTaskSpec(files={"b.py": "x = 1\n"}))
    view = server.router.get_task(tid)
    assert view is not None
    server.ledger.record(TaskView(
        request=view.request, state=TaskState.SUCCEEDED,
        assigned_node="owner",
        result=TaskResult(task_id=tid, attempt_id="",
                          state=TaskState.SUCCEEDED, output="ok")))

    counts = asyncio.run(server.clear_data())
    assert counts["sessions"] == 1
    assert counts["context_turns"] == 2
    assert counts["tasks"] == 1
    assert counts["ledger"] == 1
    assert counts["live_workspaces"] == 1
    assert counts["workspaces"] == 1
    assert counts["revoked"] == 0

    assert server.sessions.list_active() == []
    with pytest.raises(KeyError):
        server.sessions.get(sid)
    assert server.contexts.latest_version(sid) == 0
    assert server.router.get_task(tid) is None
    assert server.router.list_tasks() == []
    assert server.ledger.summary()["total_terminal"] == 0
    assert server.progress == {}
    assert server.race_groups == {}
    assert server._compacting == set()
    assert server.live_workspaces.list_ids() == []
    assert list(server.workspaces.base_dir.iterdir()) == []

    # wipe is not a one-shot: new work can be submitted afterwards
    again = server.sessions.create("owner", "echo-7b-none")
    server.router.submit(TaskRequest(
        prompt="after clear", idempotency_key="k-after"))
    assert len(server.sessions.list_active()) == 1
    assert again.session_id != sid
    assert server.router.queue_stats()["by_state"]["queued"] == 1


def test_delete_session_and_clear_queue_are_independent(tmp_path):
    from scheduler.server import SchedulerServer

    cfg = Config()
    cfg.node.data_dir = tmp_path / "n"
    cfg.server.db_path = tmp_path / "s.db"
    server = SchedulerServer(cfg)
    sid_a = server.sessions.create("owner", "echo-7b-none").session_id
    sid_b = server.sessions.create("owner", "echo-7b-none").session_id
    server.sessions.append_turn_latest(sid_a, "user", "gone")
    tid = server.router.submit(TaskRequest(prompt="keep me", idempotency_key="k-keep"))

    assert asyncio.run(server.delete_session(sid_a))
    with pytest.raises(KeyError):
        server.sessions.get(sid_a)
    assert server.contexts.latest_version(sid_a) == 0
    assert server.sessions.get(sid_b).session_id == sid_b
    assert server.router.get_task(tid) is not None

    assert asyncio.run(server.delete_task(tid))
    assert server.router.get_task(tid) is None
    assert len(server.sessions.list_active()) == 1

    tid2 = server.router.submit(TaskRequest(prompt="q", idempotency_key="k-q"))
    counts = asyncio.run(server.clear_queue())
    assert counts["tasks"] == 1
    assert server.router.get_task(tid2) is None
    assert len(server.sessions.list_active()) == 1

    counts = asyncio.run(server.clear_sessions())
    assert counts["sessions"] == 1
    assert server.sessions.list_active() == []


def test_http_dashboard_deletes(tmp_path):
    import httpx
    from scheduler.server import SchedulerServer

    cfg = Config()
    cfg.node.data_dir = tmp_path / "n"
    cfg.server.db_path = tmp_path / "s.db"
    server = SchedulerServer(cfg)
    sid = server.sessions.create("owner", "echo").session_id
    tid = server.router.submit(TaskRequest(prompt="x", idempotency_key="k-http"))

    async def run() -> None:
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(f"/v1/sessions/{sid}/delete")
            assert r.status_code == 200
            assert r.json()["deleted"] is True
            assert (await c.get(f"/v1/sessions/{sid}")).status_code == 404
            r = await c.post(f"/v1/tasks/{tid}/delete")
            assert r.status_code == 200
            sid2 = server.sessions.create("owner", "echo").session_id
            server.router.submit(TaskRequest(prompt="y", idempotency_key="k-http-2"))
            assert (await c.post("/v1/sessions/clear")).json()["sessions"] >= 1
            assert (await c.post("/v1/tasks/clear")).json()["tasks"] >= 1
            assert (await c.get("/v1/sessions")).json() == []
            assert (await c.get("/v1/tasks")).json() == []
            assert (await c.get(f"/v1/sessions/{sid2}")).status_code == 404

    asyncio.run(run())


def test_http_shutdown_does_not_500(tmp_path):
    """POST /v1/server/shutdown must resolve the actor (NameError was a 500)."""
    import httpx
    from scheduler.server import SchedulerServer

    cfg = Config()
    cfg.node.data_dir = tmp_path / "n"
    cfg.server.db_path = tmp_path / "s.db"
    server = SchedulerServer(cfg)
    server.tokens["tok"] = "n-owner"
    stopped: list[str] = []

    async def fake_now(reason: str = "server shutdown") -> None:
        stopped.append(reason)

    server.shutdown_now = fake_now  # type: ignore[method-assign]

    async def run() -> None:
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/server/shutdown", json={"confirm": True},
                             headers={"Authorization": "Bearer tok"})
            assert r.status_code == 200, r.text
            assert r.json()["shutting_down"] is True

    asyncio.run(run())
