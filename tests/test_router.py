"""Unit tests for cluster-local session routing, context replay, and
specify-vs-auto scoring. No live agents.
"""

from __future__ import annotations

from common.types import (
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
    assert wire.prompt.startswith("user: alpha unique token")
    assert "assistant: ack" in wire.prompt
    assert wire.prompt.endswith("user: beta followup")
    # queued row keeps the original user turn, not the expanded prompt
    view = router.get_task(wire.task_id)
    assert view.request.prompt == "beta followup"


def test_bind_cluster_only_when_unbound(tmp_path):
    seven = _spec(7.0)
    _, _, sessions, _ = _pool(tmp_path, ("n1", seven))
    sess = sessions.create("owner", "")
    sessions.bind_cluster(sess.session_id, "echo-7b-none")
    assert sessions.get(sess.session_id).cluster_key == "echo-7b-none"
    sessions.bind_cluster(sess.session_id, "echo-70b-none")
    assert sessions.get(sess.session_id).cluster_key == "echo-7b-none"
