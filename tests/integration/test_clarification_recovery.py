"""Tests for Clarification Lifecycle Phase P5: Recovery, Authority, and Idempotency.

Verifies Invariants:
1. Idempotent key replay recovers original request_id even after Redis is cleared.
2. Idempotent key with different payload returns 409 conflict, no second Agent launched.
3. MySQL query exception returns 503 for POST and GET (no swallowing).
4. Redis state lost: GET recommendation-requests recovers needs_clarification and question_id from MySQL.
5. Redis events lost: SSE stream recovers dispatched outbox events with stable event_id.
6. Session GET does not output legacy pending_clarifications array, fails 503 if MySQL is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from food_agent_v2.api_app import app
from food_agent_v2.d1 import api as d1_api


@pytest.fixture(autouse=True)
def clean_d1():
    d1_api._requests.clear()
    d1_api._events.clear()
    d1_api._event_cursors.clear()
    d1_api._idempotency.clear()
    yield
    d1_api._requests.clear()
    d1_api._events.clear()
    d1_api._event_cursors.clear()
    d1_api._idempotency.clear()


def test_idempotent_post_recovers_original_request_id_after_redis_evaporation():
    """同一幂等键重发已提交的选择应返回原 request_id，包括 Redis/内存被清空后。"""
    session_id = "sess_v2_rec_idem"
    key = "idem_key_replay_1"
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    orig_rid = "req_orig_12345"

    body = {
        "idempotency_key": key,
        "session_id": session_id,
        "message": "我要4个菜",
        "participants": [{"participant_ref": "p1"}],
    }
    payload_hash = d1_api._hash_payload(body)

    # 模拟 MySQL 中已存在此请求的接收记录和日志
    mock_c4 = MagicMock()
    mock_c4.load_clarification_state.return_value = {
        "session_id": session_id,
        "protocol_version": "v2",
        "active_question_id": None,
        "clarification_revision": 0,
    }
    mock_c4.load_request_acceptance.return_value = {
        "idempotency_key_hash": key_hash,
        "payload_hash": payload_hash,
        "request_id": orig_rid,
        "session_id": session_id,
        "status": "accepted",
        "created_at": 1700000000.0,
    }
    mock_c4.load_recommendation_log.return_value = {
        "request_id": orig_rid,
        "session_id": session_id,
        "status": "completed",
        "created_at": 1700000000.0,
    }
    mock_c4.load_active_clarification.return_value = None

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch.object(d1_api, "_trigger_workflow") as mock_trigger:
        client = TestClient(app)
        resp = client.post("/v1/recommendation-requests", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["request_id"] == orig_rid
        # 绝不启动第二次 Agent 工作流
        assert mock_trigger.call_count == 0


def test_idempotent_post_different_payload_conflicts():
    """同一幂等键不同载荷在 v2 下必须返回 409 冲突，不能启动第二次 Agent。"""
    session_id = "sess_v2_rec_conflict"
    key = "idem_key_conflict_1"
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()

    mock_c4 = MagicMock()
    mock_c4.load_clarification_state.return_value = {
        "session_id": session_id,
        "protocol_version": "v2",
    }
    mock_c4.load_request_acceptance.return_value = {
        "idempotency_key_hash": key_hash,
        "payload_hash": "different_hash_value",
        "request_id": "req_first_winner",
        "session_id": session_id,
        "status": "accepted",
    }

    body = {
        "idempotency_key": key,
        "session_id": session_id,
        "message": "我要新菜谱",
        "participants": [{"participant_ref": "p1"}],
    }

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch.object(d1_api, "_trigger_workflow") as mock_trigger:
        client = TestClient(app)
        resp = client.post("/v1/recommendation-requests", json=body)
        assert resp.status_code == 409
        # 不能启动工作流
        assert mock_trigger.call_count == 0


def test_mysql_query_exception_returns_503():
    """MySQL 查询异常时 POST 与 GET 返回 503，不伪装成 404 或 500。"""
    session_id = "sess_v2_db_err"
    key = "idem_key_db_err"

    mock_c4 = MagicMock()
    mock_c4.load_clarification_state.side_effect = ConnectionError("MySQL connection died")
    mock_c4.load_request_acceptance.side_effect = ConnectionError("MySQL connection died")
    mock_c4.load_recommendation_log.side_effect = ConnectionError("MySQL connection died")

    body = {
        "idempotency_key": key,
        "session_id": session_id,
        "message": "我要点菜",
        "participants": [{"participant_ref": "p1"}],
    }

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch("food_agent_v2.c4.ContextService", return_value=mock_c4):
        client = TestClient(app)
        post_resp = client.post("/v1/recommendation-requests", json=body)
        assert post_resp.status_code == 503

        get_resp = client.get("/v1/recommendation-requests/any-request-id")
        assert get_resp.status_code == 503


def test_get_recovers_needs_clarification_from_mysql_when_redis_lost():
    """Redis 请求状态丢失后，GET 从 MySQL 日志与问题表恢复 needs_clarification 和同一问题 ID。"""
    rid = "req_rec_needs_clarify"
    session_id = "sess_rec_nc"
    qid = "q_active_recover"

    mock_c4 = MagicMock()
    mock_c4.load_recommendation_log.return_value = {
        "request_id": rid,
        "session_id": session_id,
        "status": "needs_clarification",
        "created_at": 1700000000.0,
    }
    mock_c4.load_produced_active_clarification.return_value = {
        "question_id": qid,
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "question_text": "需要调整菜数",
            "options": [{"option_id": 1, "text": "2道菜"}],
        },
        "expires_at": 1700003600.0,
    }

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4):
        client = TestClient(app)
        resp = client.get(f"/v1/recommendation-requests/{rid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["request_id"] == rid
        assert data["status"] == "needs_clarification"
        assert data["session_id"] == session_id
        assert data["active_clarification"]["question_id"] == qid


def test_sse_recovers_dispatched_outbox_events_when_redis_lost():
    """已投递后 Redis 再丢失，SSE 从 outbox 恢复相同稳定 ID。"""
    rid = "req_sse_outbox_recover"
    dispatched_events = [
        {
            "event_id": f"ev_clarify_{rid}",
            "event_type": "clarification_needed",
            "seq": 1,
            "payload": {
                "question_id": f"q_{rid}",
                "question_text": "时间超限，请选择",
                "options": [{"option_id": 1, "text": "减少菜品"}],
            },
            "status": "dispatched",
        }
    ]

    mock_c4 = MagicMock()
    mock_c4.load_dispatched_outbox_events.return_value = dispatched_events

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4):
        events = d1_api.subscribe_events(rid, refresh=True)
        assert len(events) >= 1
        ev = next(e for e in events if e.get("id") == f"ev_clarify_{rid}")
        assert ev["event"] == "clarification_needed"
        data = json.loads(ev["data"])
        assert data["question_id"] == f"q_{rid}"


def test_session_get_does_not_output_old_pending_clarifications_and_fails_503_on_db_error():
    """会话 GET 不再输出旧 pending_clarifications 数组；MySQL 查询失败不能显示 active_clarification: null。"""
    session_id = "sess_v2_clean_get"

    mock_c4 = MagicMock()
    mock_c4.get_session_state.return_value = {
        "session_id": session_id,
        "participant_refs": ["p1"],
        "pending_clarifications": [{"question_id": "legacy_q"}],  # 旧数据
    }
    mock_c4.load_active_clarification.return_value = {
        "question_id": "q_active_curr",
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "question_text": "活跃问题",
            "options": [{"option_id": 1, "text": "A"}],
        },
        "expires_at": 9999999999.0,
    }

    with patch("food_agent_v2.c4.ContextService", return_value=mock_c4):
        client = TestClient(app)
        resp = client.get(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "pending_clarifications" not in data
        assert data["active_clarification"]["question_id"] == "q_active_curr"

    # 模拟 MySQL load_active_clarification 抛异常 -> 返回 503
    mock_c4.load_active_clarification.side_effect = ConnectionError("DB failure")
    with patch("food_agent_v2.c4.ContextService", return_value=mock_c4):
        client = TestClient(app)
        resp_err = client.get(f"/v1/sessions/{session_id}")
        assert resp_err.status_code == 503


def test_get_and_sse_payload_has_no_internal_or_sensitive_fields():
    """扫描 GET 与 SSE JSON 中的 modifications、private_snapshot、query_plan_snapshot、健康内部键。"""
    session_id = "sess_v2_scan_clean"
    rid = "req_v2_scan_clean"
    qid = "q_scan_clean"

    mock_c4 = MagicMock()
    mock_c4.load_recommendation_log.return_value = {
        "request_id": rid,
        "session_id": session_id,
        "status": "needs_clarification",
        "created_at": 1700000000.0,
    }
    mock_c4.load_produced_active_clarification.return_value = {
        "question_id": qid,
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "question_text": "需要调整菜数",
            "options": [{"option_id": 1, "text": "2道菜"}],
        },
        "private_snapshot": {
            "query_plan_snapshot": {"secret": "hidden"},
            "option_modifications": {1: {"taste_tags": ["清淡"]}},
        },
        "expires_at": 1700003600.0,
    }
    mock_c4.load_dispatched_outbox_events.return_value = [
        {
            "event_id": f"ev_clarify_{rid}",
            "event_type": "clarification_needed",
            "seq": 1,
            "payload": {
                "question_id": qid,
                "question_text": "需要调整菜数",
                "options": [{"option_id": 1, "text": "2道菜"}],
            },
            "status": "dispatched",
        }
    ]
    mock_c4.get_session_state.return_value = {
        "session_id": session_id,
        "participant_refs": ["p1"],
    }

    forbidden_needles = [
        "modifications",
        "private_snapshot",
        "query_plan_snapshot",
        "user_id",
        "disease_name",
        "raw_health_metrics",
    ]

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch("food_agent_v2.c4.ContextService", return_value=mock_c4):
        client = TestClient(app)

        # 1. 验证 GET /v1/recommendation-requests/{id}
        resp_req = client.get(f"/v1/recommendation-requests/{rid}")
        assert resp_req.status_code == 200
        req_text = resp_req.text
        for needle in forbidden_needles:
            assert needle not in req_text, f"Found {needle} in GET /recommendation-requests response"

        # 2. 验证 GET /v1/sessions/{id}
        resp_sess = client.get(f"/v1/sessions/{session_id}")
        assert resp_sess.status_code == 200
        sess_text = resp_sess.text
        for needle in forbidden_needles:
            assert needle not in sess_text, f"Found {needle} in GET /sessions response"

        # 3. 验证 SSE subscribe_events
        events = d1_api.subscribe_events(rid, refresh=True)
        assert len(events) >= 1
        for ev in events:
            ev_str = json.dumps(ev, ensure_ascii=False)
            for needle in forbidden_needles:
                assert needle not in ev_str, f"Found {needle} in SSE event payload"


def test_fault_matrix_post_commit_redis_failure_keeps_mysql_and_outbox_intact():
    """故障矩阵：提交后 Redis 写入失败，MySQL 提交事实与 outbox 保持完好，状态可由 MySQL 恢复。"""
    rid = "req_redis_fail_matrix"
    session_id = "sess_redis_fail_matrix"

    mock_c4 = MagicMock()
    mock_c4.load_recommendation_log.return_value = {
        "request_id": rid,
        "session_id": session_id,
        "status": "completed",
        "created_at": 1700000000.0,
    }
    mock_c4.load_dispatched_outbox_events.return_value = [
        {
            "event_id": f"ev_result_{rid}",
            "event_type": "result_committed",
            "seq": 1,
            "payload": {"request_id": rid, "menu_summary": {"plan_id": "p1"}},
            "status": "dispatched",
        }
    ]

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch("food_agent_v2.c4.redis_store.RedisSessionStore.save_request", side_effect=ConnectionError("Redis down")):
        client = TestClient(app)
        # GET 状态仍能从 MySQL 权威事实返回 200
        resp = client.get(f"/v1/recommendation-requests/{rid}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

        # SSE 仍能从 outbox 恢复
        events = d1_api.subscribe_events(rid, refresh=True)
        assert any(e.get("id") == f"ev_result_{rid}" for e in events)


def test_fault_matrix_concurrent_race_claim_acceptance():
    """输家取得原 ID；赢家执行租约有效时不得启动第二个 Agent。"""
    session_id = "sess_race_matrix"
    key = "idem_race_matrix_1"
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    winner_rid = "req_winner_race_1"

    body = {
        "idempotency_key": key,
        "session_id": session_id,
        "message": "点菜",
        "participants": [{"participant_ref": "p1"}],
    }
    payload_hash = d1_api._hash_payload(body)

    mock_c4 = MagicMock()
    mock_c4.load_clarification_state.return_value = {
        "session_id": session_id,
        "protocol_version": "v2",
    }
    # 第一次 load 未命中
    mock_c4.load_request_acceptance.return_value = None
    mock_c4.load_recommendation_log.return_value = None
    mock_c4.claim_request_execution_v2.return_value = None
    # 原子 claim 时模拟并发输掉声明，返回已存在的赢家记录
    mock_c4.claim_request_acceptance.return_value = (
        "existing",
        {
            "idempotency_key_hash": key_hash,
            "payload_hash": payload_hash,
            "request_id": winner_rid,
            "session_id": session_id,
            "status": "accepted",
            "created_at": 1700000000.0,
        },
    )

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch.object(d1_api, "_trigger_workflow") as mock_trigger:
        client = TestClient(app)
        resp = client.post("/v1/recommendation-requests", json=body)
        assert resp.status_code == 503
        assert resp.json()["request_id"] == winner_rid
        assert mock_trigger.call_count == 0


def test_plain_text_reply_auto_binds_clarification_response():
    """自然语言松绑容错：用户输入'选第一个'，网关自动绑定当前活跃澄清问题与选项。"""
    session_id = "sess_autobind_1"
    qid = "q_autobind_active"
    key = "idem_autobind_1"

    body = {
        "idempotency_key": key,
        "session_id": session_id,
        "message": "选第一个",
        "participants": [{"participant_ref": "p1"}],
    }

    mock_c4 = MagicMock()
    mock_c4.load_clarification_state.return_value = {
        "session_id": session_id,
        "protocol_version": "v2",
    }
    mock_c4.load_request_acceptance.return_value = None
    mock_c4.load_active_clarification.return_value = {
        "question_id": qid,
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "question_text": "请选择菜品数量",
            "options": [
                {"option_id": 1, "text": "2道菜"},
                {"option_id": 2, "text": "3道菜"},
            ],
        },
        "expires_at": time.time() + 3600,
    }
    mock_c4.is_clarification_committed.return_value = False
    mock_c4.claim_request_acceptance.return_value = ("winner", None)
    mock_c4.claim_request_execution_v2.return_value = {"generation": 1, "expires_at": time.time() + 60}

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch.object(d1_api, "_trigger_workflow"):
        client = TestClient(app)
        resp = client.post("/v1/recommendation-requests", json=body)
        assert resp.status_code in (200, 202)
        req_id = resp.json()["request_id"]
        # 验证底层存储的请求状态中已自动注入 clarification_response
        stored = d1_api._requests[req_id]
        assert stored.get("clarification_response") == {
            "question_id": qid,
            "option_id": 1,
        }


def test_legacy_session_returns_409_session_protocol_unsupported():
    """测试 Task 1：已有 fast_path/v1 旧会话发起新请求直接返回 409 SESSION_PROTOCOL_UNSUPPORTED。"""
    sid = "sess_legacy_v1_old"
    mock_c4 = MagicMock()
    mock_c4.load_request_acceptance.return_value = None
    mock_c4.load_clarification_state.return_value = {
        "session_id": sid,
        "workflow_mode": "fast_path",
        "protocol_version": "v1",
    }
    body = {
        "idempotency_key": "key_legacy_reject_1",
        "session_id": sid,
        "message": "我要三道菜",
        "participants": [{"participant_ref": "p1"}],
    }

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch.object(d1_api, "_trigger_workflow") as mock_trigger:
        client = TestClient(app)
        resp = client.post("/v1/recommendation-requests", json=body)
        assert resp.status_code == 409
        data = resp.json()
        assert data.get("error") == "SESSION_PROTOCOL_UNSUPPORTED"
        mock_trigger.assert_not_called()


def test_post_sessions_always_creates_langgraph_v2_even_with_fast_path_env(monkeypatch):
    """测试 Task 1：POST /v1/sessions 永远固定创建 langgraph/v2 会话，不受环境变量影响。"""
    monkeypatch.setenv("WORKFLOW_MODE", "fast_path")
    monkeypatch.setenv("CLARIFICATION_PROTOCOL", "v1")

    mock_c4 = MagicMock()
    mock_c4.create_session_record.return_value = "sess_fixed_v2_123"

    with patch("food_agent_v2.api_app.ContextService", return_value=mock_c4):
        client = TestClient(app)
        resp = client.post("/v1/sessions", json={"participants": [{"participant_ref": "p1"}]})
        assert resp.status_code == 201
        assert resp.json()["session_id"] == "sess_fixed_v2_123"
        mock_c4.create_session_record.assert_called_once_with(
            ["p1"],
            workflow_mode="langgraph",
            clarification_protocol_version="v2",
        )


def test_natural_language_binding_keeps_original_body_immutable():
    """测试 Task 2：自然语言绑定选项后，原始 body 字典保持只读，不注入 clarification_response，防止 hash 漂移。"""
    sid = "sess_immutability_test"
    qid = "q_active_opt"
    mock_c4 = MagicMock()
    mock_c4.load_request_acceptance.return_value = None
    mock_c4.load_clarification_state.return_value = {
        "session_id": sid,
        "workflow_mode": "langgraph",
        "protocol_version": "v2",
    }
    mock_c4.load_active_clarification.return_value = {
        "question_id": qid,
        "session_id": sid,
        "status": "pending",
        "public_payload": {
            "question_text": "您的偏好？",
            "options": [
                {"option_id": 1, "text": "清淡家常"},
                {"option_id": 2, "text": "香辣浓郁"},
            ],
        },
        "expires_at": 9999999999.0,
    }
    mock_c4.is_clarification_committed.return_value = False
    mock_c4.claim_request_acceptance.return_value = ("winner", None)
    mock_c4.claim_request_execution_v2.return_value = {"generation": 1}

    orig_body = {
        "idempotency_key": "key_immutable_1",
        "session_id": sid,
        "message": "按第1个来",
        "participants": [{"participant_ref": "p1"}],
    }
    orig_copy = dict(orig_body)

    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch.object(d1_api, "_trigger_workflow"):
        code, resp = d1_api.create_request(orig_body)
        assert code == 202
        req_id = resp["request_id"]
        # 1. 验证传入的原始 body 未被就地改写
        assert orig_body == orig_copy
        assert "clarification_response" not in orig_body

        # 2. 验证存储的内部请求状态获得了派生的 clarification_response
        stored = d1_api._requests[req_id]
        assert stored.get("clarification_response") == {
            "question_id": qid,
            "option_id": 1,
        }


def test_unknown_session_is_rejected_before_acceptance_claim():
    body = {
        "idempotency_key": "key_unknown_session_v2",
        "session_id": "sess_does_not_exist",
        "message": "推荐晚餐",
        "participants": [{"participant_ref": "p1"}],
    }
    mock_c4 = MagicMock()
    mock_c4.load_request_acceptance.return_value = None
    mock_c4.load_clarification_state.return_value = None
    mock_c4.claim_request_acceptance.return_value = ("winner", None)
    mock_c4.claim_request_execution_v2.return_value = {"generation": 1}
    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch("food_agent_v2.c4.redis_store.RedisSessionStore") as redis_cls, \
         patch.object(d1_api, "_create_new", return_value=(202, {"status": "accepted"})):
        redis_cls.return_value.claim_idempotency.return_value = ("winner", {})
        code, response = d1_api.create_request(body)

    assert code == 404
    assert response["error"] == "SESSION_NOT_FOUND"
    mock_c4.claim_request_acceptance.assert_not_called()


def test_internal_create_error_does_not_retry_without_execution_generation():
    body = {
        "idempotency_key": "key_create_error_v2",
        "session_id": "sess_existing_v2",
        "message": "推荐晚餐",
        "participants": [{"participant_ref": "p1"}],
    }
    mock_c4 = MagicMock()
    mock_c4.load_request_acceptance.return_value = None
    mock_c4.load_clarification_state.return_value = {
        "session_id": body["session_id"],
        "workflow_mode": "langgraph", "protocol_version": "v2",
    }
    mock_c4.load_active_clarification.return_value = None
    mock_c4.claim_request_acceptance.return_value = ("winner", None)
    mock_c4.claim_request_execution_v2.return_value = {"generation": 1}
    with patch("food_agent_v2.d1.ContextService", return_value=mock_c4), \
         patch("food_agent_v2.c4.redis_store.RedisSessionStore") as redis_cls, \
         patch.object(d1_api, "_create_new", side_effect=TypeError("internal failure")) as create:
        redis_cls.return_value.claim_idempotency.return_value = ("winner", {})
        code, response = d1_api.create_request(body)

    assert code == 503
    assert response["error"] == "REQUEST_RECOVERY_PENDING"
    assert create.call_count == 1
    assert create.call_args.kwargs["execution_generation"] == 1
