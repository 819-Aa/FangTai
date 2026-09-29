"""Execution generation must fence v2 commits and lock recovery."""

import threading
from types import SimpleNamespace
from unittest.mock import patch

from food_agent_v2.c3.graph_orchestrator import LangGraphRecommendationOrchestrator
from food_agent_v2.c3.state import RequestStatus, WorkflowError, WorkflowState
from food_agent_v2.c3.tool_handler import ToolContext


def test_v2_clarification_commit_carries_execution_generation() -> None:
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    state = WorkflowState(
        request_id="request-1", build_id="build-1",
        status=RequestStatus.NEEDS_CLARIFICATION,
        participant_refs=["p1"], shared_context_ref="session-v2",
        error=WorkflowError("constraint_conflict", "请先选择"),
    )
    captured = []
    with patch("food_agent_v2.application.commit_request_result", side_effect=lambda **kw: captured.append(kw)), \
         patch("food_agent_v2.c3.runtime.d1_api.update_status"), \
         patch.object(runner, "_session_lock_held", return_value=True):
        runner._finalize(
            state, "request-1", SimpleNamespace(
                renew_request_execution_v2=lambda *_args: True,
            ), "458",
            execution_owner="owner-2", execution_generation=2,
        )

    assert captured[0]["execution_owner"] == "owner-2"
    assert captured[0]["execution_generation"] == 2


def test_v2_failed_workflow_commits_authoritative_terminal() -> None:
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    state = WorkflowState(
        request_id="request-1", build_id="build-1",
        status=RequestStatus.FAILED,
        participant_refs=["p1"], shared_context_ref="session-v2",
        error=WorkflowError("MODEL_INVOCATION_FAILED", "network unavailable"),
    )
    captured = []
    with patch("food_agent_v2.application.commit_request_result", side_effect=lambda **kw: captured.append(kw)), \
         patch("food_agent_v2.c3.runtime.d1_api.update_status"), \
         patch("food_agent_v2.c3.runtime.d1_api.publish_terminal"), \
         patch.object(runner, "_session_lock_held", return_value=True):
        runner._finalize(
            state, "request-1", SimpleNamespace(
                renew_request_execution_v2=lambda *_args: True,
            ), "458",
            execution_owner="owner-2", execution_generation=2,
        )

    assert len(captured) == 1
    assert captured[0]["status"] == "failed"
    assert captured[0]["error_code"] == "MODEL_INVOCATION_FAILED"
    assert captured[0]["execution_generation"] == 2


def test_reclaimed_v2_worker_sees_old_session_lock_as_recoverable() -> None:
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    statuses = []
    with patch.object(runner, "_get_c4", return_value=SimpleNamespace(
        acquire_session_lock=lambda *_args: None,
    )), patch.object(runner, "_finalize") as finalize, \
         patch("food_agent_v2.c3.runtime.d1_api.update_status", side_effect=lambda *args, **kwargs: statuses.append((args, kwargs))):
        runner.run(
            "request-1", "session-v2", "菜单", [],
            execution_owner="owner-2", execution_generation=2,
        )

    finalize.assert_not_called()
    assert statuses[0][0] == ("request-1", "recovery_required")


def test_v2_execution_heartbeat_marks_lost_generation() -> None:
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    lost = threading.Event()
    stop = threading.Event()
    c4 = SimpleNamespace(renew_request_execution_v2=lambda *_args: False)

    heartbeat = runner._start_execution_heartbeat(
        c4, "request-1", "owner-1", 1, lost, stop,
    )
    heartbeat.join(timeout=2)

    assert lost.is_set()


def test_lost_execution_stops_redis_lock_heartbeat() -> None:
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    lost = threading.Event()
    lost.set()
    stop = threading.Event()
    calls = []
    heartbeat = threading.Thread(
        target=runner._heartbeat_loop,
        args=(lambda *_args: calls.append(True), "session-v2", "458", lost, stop, 0.01),
    )
    heartbeat.start()
    heartbeat.join(timeout=0.05)
    stop.set()
    heartbeat.join(timeout=1)

    assert calls == []


def test_v2_execution_deadline_stops_renewal() -> None:
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    lost = threading.Event()
    stop = threading.Event()
    calls = []
    runner._heartbeat_loop(
        lambda *_args: calls.append(True), "request-1", "owner-1",
        lost, stop, 0.01, deadline=0.0,
    )

    assert lost.is_set()
    assert calls == []


def test_expired_v2_execution_cannot_commit_even_before_database_lease_expires() -> None:
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    runner._execution_lost = threading.Event()
    runner._execution_lost.set()
    state = WorkflowState(
        request_id="request-1", status=RequestStatus.FAILED,
        error=WorkflowError("SESSION_LOCK_LOST", "execution expired"),
    )
    c4 = SimpleNamespace(renew_request_execution_v2=lambda *_args: True)
    with patch("food_agent_v2.application.commit_request_result") as commit, \
         patch("food_agent_v2.c3.runtime.d1_api.update_status") as update:
        result = runner._finalize(
            state, "request-1", c4,
            execution_owner="owner-1", execution_generation=1,
        )

    assert result == "recovery_required"
    commit.assert_not_called()
    update.assert_not_called()


def test_lost_execution_stops_before_context_build() -> None:
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    lost = threading.Event()
    lost.set()
    calls = []
    c4 = SimpleNamespace(build_shared_context=lambda *_args, **_kwargs: calls.append(True))
    state = {
        "request_id": "request-1", "session_id": "session-v2", "message": "菜单",
        "participant_refs": [], "user_id_mapping": {}, "build_id": "build-1",
        "c4": c4, "lock_token": "458", "lost": lost,
        "workflow_state": WorkflowState(
            request_id="request-1", build_id="build-1", status=RequestStatus.RUNNING,
        ),
        "tool_context": ToolContext(request_id="request-1", build_id="build-1"),
    }
    with patch.object(runner, "_is_cancelled", return_value=False):
        result = runner._node_understand_intent(state)

    assert result["is_terminal"] is True
    assert calls == []
