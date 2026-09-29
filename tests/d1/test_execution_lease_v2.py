"""A reclaimed v2 request must keep its execution generation across D1."""

import threading
from unittest.mock import MagicMock, patch

from food_agent_v2.d1 import RecommendationAPI, _active_execution
from food_agent_v2.d1.schemas import SSEEventType


def test_reclaimed_v2_request_passes_claimed_generation_to_workflow() -> None:
    api = RecommendationAPI()
    body = {
        "idempotency_key": "same-key", "session_id": "session-v2",
        "message": "推荐晚餐", "participants": [],
    }
    existing = {
        "request_id": "request-1", "session_id": "session-v2",
        "payload_hash": api._hash_payload(body), "status": "recovery_required",
    }
    c4 = MagicMock()
    c4.load_recommendation_log.return_value = None
    c4.claim_request_execution_v2.return_value = {
        "owner": "owner-2", "generation": 2, "lease_until": 9999999999.0,
    }
    with patch("food_agent_v2.d1.uuid.uuid4") as uuid4, \
         patch.object(api, "_create_new", return_value=(202, {"request_id": "request-1"})) as create:
        uuid4.return_value.hex = "owner-2"
        status, _ = api._resolve_v2_existing(existing, body, [], c4)

    assert status == 202
    assert create.call_args.kwargs["execution_owner"] == "owner-2"
    assert create.call_args.kwargs["execution_generation"] == 2


def test_reclaimed_v2_start_failure_releases_generation_for_retry() -> None:
    api = RecommendationAPI()
    body = {
        "idempotency_key": "same-key", "session_id": "session-v2",
        "message": "推荐晚餐", "participants": [],
    }
    existing = {
        "request_id": "request-1", "session_id": "session-v2",
        "payload_hash": api._hash_payload(body), "status": "recovery_required",
    }
    c4 = MagicMock()
    c4.load_recommendation_log.return_value = None
    c4.claim_request_execution_v2.return_value = {
        "owner": "owner-2", "generation": 2, "lease_until": 9999999999.0,
    }
    with patch.object(api, "_create_new", side_effect=TypeError("start failed")) as create:
        status, result = api._resolve_v2_existing(existing, body, [], c4)

    assert status == 503
    assert result == {
        "error": "REQUEST_RECOVERY_PENDING", "request_id": "request-1",
        "session_id": "session-v2",
        "message": "请求启动未完成，请使用相同幂等键与载荷重试",
    }
    assert create.call_count == 1
    c4.finish_request_execution_v2.assert_called_once_with(
        "request-1", create.call_args.kwargs["execution_owner"], 2, False,
    )


def test_v2_retry_with_active_claim_does_not_start_workflow() -> None:
    api = RecommendationAPI()
    body = {
        "idempotency_key": "same-key", "session_id": "session-v2",
        "message": "推荐晚餐", "participants": [],
    }
    existing = {
        "request_id": "request-1", "session_id": "session-v2",
        "payload_hash": api._hash_payload(body), "status": "running",
    }
    c4 = MagicMock()
    c4.load_recommendation_log.return_value = None
    c4.claim_request_execution_v2.return_value = None
    with patch.object(api, "_create_new") as create, patch.object(api, "_restore_request"):
        status, result = api._resolve_v2_existing(existing, body, [], c4)

    assert status == 503
    assert result["request_id"] == "request-1"
    create.assert_not_called()


def test_stale_v2_worker_cannot_publish_temporary_event_or_status(monkeypatch) -> None:
    api = RecommendationAPI()
    request_id = "request-stale"
    api._requests[request_id] = {
        "request_id": request_id, "session_id": "session-v2",
        "status": "running", "stage_events_cursor": 0,
    }
    api._events[request_id] = []
    api._event_cursors[request_id] = 1
    c4 = MagicMock()
    c4.load_clarification_state.return_value = {
        "protocol_version": "v2", "workflow_mode": "langgraph",
    }
    c4.renew_request_execution_v2.return_value = False
    c4.load_recommendation_log.return_value = None
    observed = []

    class FakeRunner:
        def run(self, **_kwargs):
            api.publish_analysis_event(request_id, "query_understanding", "旧代次", [])
            api.update_status(request_id, "failed", error={"code": "OLD", "message": "stale"})
            observed.append(True)

    class SynchronousThread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(threading, "Thread", SynchronousThread)
    monkeypatch.setattr(api, "_persist_request", lambda *args, **kwargs: None)
    with patch("food_agent_v2.d1.ContextService", return_value=c4), \
         patch("food_agent_v2.c3.graph_orchestrator.LangGraphRecommendationOrchestrator", return_value=FakeRunner()):
        api._trigger_workflow(
            request_id, "session-v2", {"message": "菜单"},
            execution_owner="old", execution_generation=1,
        )

    assert observed == [True]
    assert api._events[request_id] == []
    assert api._requests[request_id]["status"] == "running"


def test_redis_generation_rejection_rolls_back_local_projection(monkeypatch) -> None:
    api = RecommendationAPI()
    request_id = "request-stale"
    api._requests[request_id] = {
        "request_id": request_id, "status": "running",
        "execution_generation": 1, "stage_events_cursor": 0,
    }
    api._events[request_id] = []
    api._event_cursors[request_id] = 1
    store = MagicMock()
    store.save_request_fenced.return_value = False
    monkeypatch.setattr(api, "_can_project", lambda _rid: True)
    with patch("food_agent_v2.c4.redis_store.RedisSessionStore", return_value=store):
        api._emit_event(request_id, SSEEventType.ANALYSIS_READY, {"stage": "old"})
        api.update_status(request_id, "failed", error={"code": "OLD", "message": "stale"})

    assert api._events[request_id] == []
    assert api._requests[request_id]["status"] == "running"
    assert api._requests[request_id]["stage_events_cursor"] == 0


def test_get_and_sse_hide_previous_generation_after_reclaim() -> None:
    api = RecommendationAPI()
    request_id = "request-reclaimed"
    api._requests[request_id] = {
        "request_id": request_id, "session_id": "session-v2",
        "created_at": "2026-09-29T00:00:00Z", "status": "failed",
        "execution_owner": "old", "execution_generation": 1,
        "error": {"code": "OLD", "message": "stale"},
    }
    api._events[request_id] = [{
        "id": "1", "event": "analysis_ready", "data": '{"stage":"old"}',
    }]
    c4 = MagicMock()
    c4.load_recommendation_log.return_value = None
    c4.load_request_acceptance_by_request_id.return_value = {
        "request_id": request_id, "status": "running",
        "execution_owner": "new", "execution_generation": 2,
        "execution_lease_until": 9999999999.0,
    }
    c4.load_dispatched_outbox_events.return_value = []
    with patch("food_agent_v2.d1.ContextService", return_value=c4):
        code, status = api.get_request_status(request_id)
        events = api.subscribe_events(request_id)

    assert code == 503
    assert status["request_id"] == request_id
    assert events == []


def test_expired_v2_worker_cannot_renew_for_temporary_projection() -> None:
    api = RecommendationAPI()
    c4 = MagicMock()
    token = _active_execution.set({
        "request_id": "request-1", "owner": "owner-1", "generation": 1,
        "committed": False, "lost": False, "deadline": 0.0,
    })
    try:
        with patch("food_agent_v2.d1.ContextService", return_value=c4):
            allowed = api._can_project("request-1")
    finally:
        _active_execution.reset(token)

    assert allowed is False
    c4.renew_request_execution_v2.assert_not_called()
