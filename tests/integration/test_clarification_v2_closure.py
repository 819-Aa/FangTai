"""Regression coverage for v2 protocol isolation and durable projections."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from food_agent_v2.api_app import app
from food_agent_v2.d1 import RecommendationAPI


def test_public_session_request_cannot_choose_workflow_protocol():
    with patch("food_agent_v2.c4.ContextService") as context_type:
        context_type.return_value.create_session_record.return_value = "sess_should_not_exist"
        response = TestClient(app).post("/v1/sessions", json={
            "participants": [{"participant_ref": "p1"}],
            "workflow_mode": "langgraph",
            "clarification_protocol_version": "v2",
        })

    assert response.status_code == 422
    context_type.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "protocol", "expected_mode", "expected_protocol"),
    [
        ("fast_path", "v2", "langgraph", "v2"),
        ("langgraph", "v2", "langgraph", "v2"),
    ],
)
def test_session_protocol_is_selected_by_server(
    monkeypatch, mode, protocol, expected_mode, expected_protocol,
):
    monkeypatch.setenv("WORKFLOW_MODE", mode)
    monkeypatch.setenv("CLARIFICATION_PROTOCOL", protocol)
    context = MagicMock()
    context.create_session_record.return_value = "sess_server_selected"

    with patch("food_agent_v2.c4.ContextService", return_value=context):
        response = TestClient(app).post("/v1/sessions", json={
            "participants": [{"participant_ref": "p1"}],
        })

    assert response.status_code == 201
    context.create_session_record.assert_called_once_with(
        ["p1"], workflow_mode=expected_mode,
        clarification_protocol_version=expected_protocol,
    )




def test_implicit_session_storage_failure_does_not_claim_idempotency():
    api = RecommendationAPI()
    context = MagicMock()
    context.ensure_session_record.side_effect = ConnectionError("db down")
    body = {
        "idempotency_key": "key_new", "message": "推荐晚餐",
        "participants": [{"participant_ref": "p1", "label": "用户 1"}],
    }
    with patch("food_agent_v2.d1.ContextService", return_value=context), \
         patch("food_agent_v2.d1.resolve_participants", return_value=(
             [{"participant_ref": "p1", "user_id": 1}], [])), \
         patch.object(context, "claim_request_acceptance") as claim:
        code, response = api.create_request(body)
    assert code == 503
    assert response["error"] == "DATABASE_UNAVAILABLE"
    claim.assert_not_called()


def test_old_request_does_not_project_next_active_question():
    api = RecommendationAPI()
    context = MagicMock()
    context.load_recommendation_log.return_value = {
        "request_id": "req_q1", "session_id": "sess_chain",
        "status": "needs_clarification", "created_at": 1700000000.0,
    }
    context.load_produced_active_clarification.return_value = None
    context.load_active_clarification.return_value = {
        "question_id": "q_req_q2", "session_id": "sess_chain",
        "status": "pending", "public_payload": {
            "question_text": "Q2", "options": [{"option_id": 1, "text": "继续"}],
        },
    }

    with patch("food_agent_v2.d1.ContextService", return_value=context), \
         patch.object(api, "_restore_request"):
        code, body = api.get_request_status("req_q1")

    assert code == 200
    assert body.get("active_clarification") is None
    context.load_produced_active_clarification.assert_called_once_with("req_q1")


def test_mysql_error_does_not_return_cached_terminal_status():
    api = RecommendationAPI()
    api._requests["req_cached"] = {
        "request_id": "req_cached", "session_id": "sess_v2",
        "status": "completed", "created_at": "2026-09-28T00:00:00Z",
    }
    context = MagicMock()
    context.load_recommendation_log.side_effect = ConnectionError("db down")

    with patch("food_agent_v2.d1.ContextService", return_value=context):
        code, body = api.get_request_status("req_cached")

    assert code == 503
    assert body["error"] == "DATABASE_ERROR"


def test_outbox_recovery_uses_public_sse_shape():
    api = RecommendationAPI()
    context = MagicMock()
    context.load_dispatched_outbox_events.return_value = [{
        "event_id": "ev_clarify_req1", "event_type": "clarification_needed",
        "payload": {
            "question_id": "q_req1", "question_text": "选一个",
            "options": [{"option_id": 1, "text": "方案一"}],
            "inquiry_category": "INTERNAL_REASON",
            "expires_at": 1893456000.0,
        },
    }]

    with patch("food_agent_v2.d1.ContextService", return_value=context), \
         patch.object(api, "_refresh_events"):
        events = api.subscribe_events("req1", refresh=True)

    event = next(item for item in events if item["id"] == "ev_clarify_req1")
    payload = json.loads(event["data"])
    assert payload == {
        "request_id": "req1", "question_id": "q_req1",
        "clarification": "选一个",
        "options": [{"option_id": 1, "text": "方案一"}],
        "expires_at": 1893456000.0,
    }


def test_accepted_v2_request_is_reclaimed_after_prestart_crash():
    api = RecommendationAPI()
    body = {"session_id": "sess_v2", "message": "菜单", "participants": [], "idempotency_key": "key"}
    accepted = {
        "request_id": "req_original", "session_id": "sess_v2",
        "payload_hash": api._hash_payload(body), "status": "accepted",
        "created_at": 1700000000.0,
    }
    context = MagicMock()
    context.load_recommendation_log.return_value = None
    context.claim_request_execution_v2.return_value = {
        "owner": "claimed-owner", "generation": 1, "lease_until": 1893456000.0,
    }
    with patch("food_agent_v2.d1.ContextService", return_value=context), \
         patch.object(api, "_create_new", return_value=(202, {"request_id": "req_original"})) as start:
        code, response = api._resolve_v2_existing(accepted, body, [], context)
    assert (code, response["request_id"]) == (202, "req_original")
    start.assert_called_once()
    assert start.call_args.args[0] == "req_original"


def test_unavailable_v2_execution_returns_recoverable_error_with_original_id():
    api = RecommendationAPI()
    body = {"session_id": "sess_v2", "message": "菜单", "participants": [], "idempotency_key": "key"}
    accepted = {
        "request_id": "req_original", "session_id": "sess_v2",
        "payload_hash": api._hash_payload(body), "status": "running",
    }
    context = MagicMock()
    context.load_recommendation_log.return_value = None
    context.claim_request_execution_v2.return_value = None
    with patch.object(api, "_restore_request"):
        code, response = api._resolve_v2_existing(accepted, body, [], context)
    assert code == 503
    assert response["request_id"] == "req_original"


def test_recovered_result_event_matches_public_shape_and_cursor():
    api = RecommendationAPI()
    context = MagicMock()
    context.load_dispatched_outbox_events.return_value = [
        {"event_id": "ev_clarify_req1", "event_type": "clarification_needed",
         "payload": {"question_id": "q1", "question_text": "选一个", "options": []}},
        {"event_id": "ev_result_req1", "event_type": "result_committed",
         "payload": {"menu_summary": {"plan_id": "p1"}}},
    ]
    with patch("food_agent_v2.d1.ContextService", return_value=context), \
         patch.object(api, "_refresh_events"):
        events = api.subscribe_events("req1", last_event_id="ev_clarify_req1", refresh=True)
    assert len(events) == 1
    assert events[0]["id"] == "ev_result_req1"
    assert json.loads(events[0]["data"]) == {
        "request_id": "req1", "menu_summary": {"plan_id": "p1"},
    }
