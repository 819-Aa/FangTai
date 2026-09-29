"""Tests for Clarification Lifecycle Phase P0-P4 Specification Compliance.

Verifies Invariants I1 through I7:
I1: Unbound Client Reply Identity Prevention (Structured clarification_response)
I2: Commit-Before-Publish (No premature Redis / SSE exposure before MySQL commit)
I3: Single-Winner Atomic Lifecycle Transition (MySQL row locking)
I5 & I6: Public Projection Redaction (No leakage of internal modifications / query_plan_snapshot)
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from food_agent_v2.api_app import app
from food_agent_v2.c3.graph_orchestrator import LangGraphRecommendationOrchestrator
from food_agent_v2.c3.state import RequestStatus, WorkflowState
from food_agent_v2.c3.tool_handler import ToolContext
from food_agent_v2.contracts.clarification import (
    ClarificationOptionView,
    ClarificationPublicPayload,
    ClarificationResponseInput,
    ClarificationView,
)
from food_agent_v2.d1.schemas import validate_create_request


def test_clarification_response_schema_validation():
    """Verify ClarificationResponseInput validates question_id and option_id strictly."""
    # Valid structured input
    resp = ClarificationResponseInput(question_id="q_123", option_id=2)
    assert resp.question_id == "q_123"
    assert resp.option_id == 2

    # Option id must be positive int
    with pytest.raises(ValidationError):
        ClarificationResponseInput(question_id="q_123", option_id=0)

    # Question id cannot be empty
    with pytest.raises(ValidationError):
        ClarificationResponseInput(question_id="   ", option_id=1)


def test_d1_create_request_schema_forbids_injected_modifications():
    """Verify D1 request schema strictly rejects injected modifications or snapshots."""
    # Valid payload with clarification_response
    valid_payload = {
        "idempotency_key": str(uuid.uuid4()),
        "message": "确认",
        "participants": [{"participant_ref": "p1", "label": "用户 1"}],
        "clarification_response": {
            "question_id": "q_123",
            "option_id": 1,
        },
    }
    err = validate_create_request(valid_payload)
    assert err is None

    # Rejected if client tries to smuggle modifications
    invalid_payload = {
        "idempotency_key": str(uuid.uuid4()),
        "message": "确认",
        "participants": [{"participant_ref": "p1", "label": "用户 1"}],
        "clarification_response": {
            "question_id": "q_123",
            "option_id": 1,
            "modifications": {"dish_count_requested": 10},
        },
    }
    err = validate_create_request(invalid_payload)
    assert err is not None
    assert err["error"] == "VALIDATION_FAILED"
    assert any("forbidden internal field" in d["issue"] for d in err["details"])


def test_public_clarification_view_redacts_private_data():
    """Verify ClarificationView strips modifications and query_plan_snapshot."""
    pub_payload = ClarificationPublicPayload(
        question_text="您想要几道菜？",
        options=[
            ClarificationOptionView(
                option_id=1,
                text="推荐 2 道菜",
            ),
            ClarificationOptionView(
                option_id=2,
                text="推荐 3 道菜",
            ),
        ],
        inquiry_category="SAFE_CANDIDATE_SHORTAGE",
    )

    view = ClarificationView.from_public_payload("q_100", pub_payload, expires_at=123456.0)
    view_dict = view.model_dump()

    # Public view must have question_id and options
    assert view_dict["question_id"] == "q_100"
    assert len(view_dict["options"]) == 2
    # Public view options MUST NOT contain modifications!
    for opt in view_dict["options"]:
        assert "option_id" in opt
        assert "text" in opt
        assert "modifications" not in opt


def test_graph_understand_intent_structured_response_success(monkeypatch):
    """Verify structured clarification_response matching active MySQL question is applied."""
    monkeypatch.setenv("CLARIFICATION_PROTOCOL", "v2")
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    session_id = "sess_structured_ok"
    qid = "q_active_1"

    active_record = {
        "question_id": qid,
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "question_text": "时间超限，请选择：",
            "options": [
                {"option_id": 1, "text": "放宽时间", "modifications": {"time_constraint_policy": "relax"}},
                {"option_id": 2, "text": "减少菜品", "modifications": {"dish_count_requested": 2}},
            ],
        },
        "private_snapshot": {
            "query_plan_snapshot": {
                "request_id": str(uuid.uuid4()),
                "dish_count_requested": 4,
                "meal_types": ["dinner"],
                "participant_refs": ["p1"],
            },
            "current_menu_version": "plan-prev",
        },
        "expires_at": 9999999999.0,
    }

    mock_c4 = SimpleNamespace(
        build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=session_id), None),
        validate_context_integrity=lambda *args, **kwargs: {"valid": True},
        get_session_state=lambda sid: {"current_menu": {}},
        load_active_clarification=lambda sid: active_record,
        load_clarification_state=lambda sid: {"protocol_version": None, "clarification_revision": 0},
        is_clarification_committed=lambda sid, q: False,
        get_pending_clarifications=lambda sid: [],
        consume_pending_clarification=lambda *args, **kwargs: None,
    )

    state = {
        "request_id": str(uuid.uuid4()),
        "session_id": session_id,
        "build_id": str(uuid.uuid4()),
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "lock_token": "token-test",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": mock_c4,
        "message": "确认选项",
        "clarification_response": {"question_id": qid, "option_id": 2},
        "workflow_state": WorkflowState(
            request_id=str(uuid.uuid4()),
            build_id=str(uuid.uuid4()),
            status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        ),
        "tool_context": ToolContext(request_id=str(uuid.uuid4()), build_id=str(uuid.uuid4())),
    }

    res = runner._node_understand_intent(state)
    assert not res.get("is_terminal")
    assert res.get("pending_clarification_to_consume") == qid
    assert res.get("selected_option_id") == 2
    # Modifications applied: dish_count_requested changed from 4 to 2
    qp = res.get("query_plan")
    assert qp is not None
    assert qp.dish_count_requested == 2


def test_graph_understand_intent_stale_question_rejected():
    """Verify submitting a stale/mismatched question_id fails closed with CLARIFICATION_STALE."""
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    session_id = "sess_stale"

    active_record = {
        "question_id": "q_active_new",
        "session_id": session_id,
        "status": "pending",
        "public_payload": {"options": [{"option_id": 1, "text": "A", "modifications": {}}]},
        "expires_at": 9999999999.0,
    }

    mock_c4 = SimpleNamespace(
        build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=session_id), None),
        validate_context_integrity=lambda *args, **kwargs: {"valid": True},
        get_session_state=lambda sid: {"current_menu": {}},
        load_active_clarification=lambda sid: active_record,
        is_clarification_committed=lambda sid, q: False,
        get_pending_clarifications=lambda sid: [],
    )

    state = {
        "request_id": str(uuid.uuid4()),
        "session_id": session_id,
        "build_id": str(uuid.uuid4()),
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "lock_token": "token-test",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": mock_c4,
        "message": "回复",
        "clarification_response": {"question_id": "q_old_stale", "option_id": 1},
        "workflow_state": WorkflowState(
            request_id=str(uuid.uuid4()),
            build_id=str(uuid.uuid4()),
            status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        ),
        "tool_context": ToolContext(request_id=str(uuid.uuid4()), build_id=str(uuid.uuid4())),
    }

    res = runner._node_understand_intent(state)
    assert res.get("is_terminal") is True
    assert res.get("error_code") == "CLARIFICATION_STALE"
    wf_state = res.get("workflow_state")
    assert wf_state.status == RequestStatus.FAILED
    assert wf_state.error.error_code == "CLARIFICATION_STALE"


def test_graph_understand_intent_option_out_of_range():
    """Verify option_id out of range fails closed with OPTION_OUT_OF_RANGE."""
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    session_id = "sess_range"
    qid = "q_range_test"

    active_record = {
        "question_id": qid,
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "options": [
                {"option_id": 1, "text": "A", "modifications": {}},
                {"option_id": 2, "text": "B", "modifications": {}},
            ]
        },
        "expires_at": 9999999999.0,
    }

    mock_c4 = SimpleNamespace(
        build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=session_id), None),
        validate_context_integrity=lambda *args, **kwargs: {"valid": True},
        get_session_state=lambda sid: {"current_menu": {}},
        load_active_clarification=lambda sid: active_record,
        is_clarification_committed=lambda sid, q: False,
        get_pending_clarifications=lambda sid: [],
    )

    state = {
        "request_id": str(uuid.uuid4()),
        "session_id": session_id,
        "build_id": str(uuid.uuid4()),
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "lock_token": "token-test",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": mock_c4,
        "message": "回复",
        "clarification_response": {"question_id": qid, "option_id": 99},
        "workflow_state": WorkflowState(
            request_id=str(uuid.uuid4()),
            build_id=str(uuid.uuid4()),
            status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        ),
        "tool_context": ToolContext(request_id=str(uuid.uuid4()), build_id=str(uuid.uuid4())),
    }

    res = runner._node_understand_intent(state)
    assert res.get("is_terminal") is True
    assert res.get("error_code") == "OPTION_OUT_OF_RANGE"
    assert res.get("workflow_state").error.error_code == "OPTION_OUT_OF_RANGE"


def test_graph_understand_intent_protocol_v2_requires_structured_response(monkeypatch):
    """Persisted v2 stays strict even after the process environment changes."""
    monkeypatch.setenv("CLARIFICATION_PROTOCOL", "legacy")
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    session_id = "sess_v2_strict"
    qid = "q_active_v2"

    active_record = {
        "question_id": qid,
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "options": [
                {"option_id": 1, "text": "选第一个", "modifications": {}},
                {"option_id": 2, "text": "选第二个", "modifications": {}},
            ]
        },
        "expires_at": 9999999999.0,
    }

    mock_c4 = SimpleNamespace(
        build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=session_id), None),
        validate_context_integrity=lambda *args, **kwargs: {"valid": True},
        get_session_state=lambda sid: {"current_menu": {}},
        load_active_clarification=lambda sid: active_record,
        load_clarification_state=lambda sid: {"protocol_version": "v2", "clarification_revision": 0},
        is_clarification_committed=lambda sid, q: False,
        get_pending_clarifications=lambda sid: [],
    )

    state = {
        "request_id": str(uuid.uuid4()),
        "session_id": session_id,
        "build_id": str(uuid.uuid4()),
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "lock_token": "token-test",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": mock_c4,
        "message": "选第一个",  # Plain text reply without clarification_response
        "clarification_response": None,
        "workflow_state": WorkflowState(
            request_id=str(uuid.uuid4()),
            build_id=str(uuid.uuid4()),
            status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        ),
        "tool_context": ToolContext(request_id=str(uuid.uuid4()), build_id=str(uuid.uuid4())),
    }

    res = runner._node_understand_intent(state)
    assert res.get("is_terminal") is True
    assert res.get("error_code") == "CLARIFICATION_REFERENCE_REQUIRED"


def test_api_session_projection_redacts_private_snapshot():
    """Verify GET /v1/sessions/{id} returns sanitized active clarification without private leaks."""
    from fastapi.testclient import TestClient

    client = TestClient(app)

    session_id = "sess_redact_test"
    active_data = {
        "question_id": "q_test_123",
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "question_text": "需要澄清",
            "options": [
                {"option_id": 1, "text": "方案 A", "modifications": {"internal_secret": 123}},
            ],
            "inquiry_category": "TIME_LIMIT_EXCEEDED",
        },
        "private_snapshot": {
            "query_plan_snapshot": {"secret_constraints": ["private_1"]},
            "current_menu_version": "v1",
        },
        "expires_at": 9999999999.0,
    }

    with patch("food_agent_v2.c4.ContextService") as mock_c4_cls:
        c4_inst = MagicMock()
        c4_inst.load_active_clarification.return_value = active_data
        c4_inst.get_session_state.return_value = {}
        c4_inst.get_pending_clarifications.return_value = []
        mock_c4_cls.return_value = c4_inst

        resp = client.get(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200
        data = resp.json()

        # Active clarification must be projected cleanly
        active = data.get("active_clarification")
        assert active is not None
        assert active["question_id"] == "q_test_123"
        assert active["question_text"] == "需要澄清"
        assert len(active["options"]) == 1
        opt = active["options"][0]
        assert opt["option_id"] == 1
        assert opt["text"] == "方案 A"

        # FORBIDDEN: modifications, query_plan_snapshot, private_snapshot MUST NOT be present
        assert "modifications" not in opt
        assert "private_snapshot" not in active
        assert "query_plan_snapshot" not in active
        assert "secret_constraints" not in json.dumps(data)


def test_v2_redis_stale_mysql_active_selects_mysql_question():
    """Redis 有过期/已消费的旧问题、MySQL 指向新问题时只能选择新问题。
    若传入旧问题的 question_id，必须拒绝（CLARIFICATION_STALE），不得从 Redis 兜底执行。
    """
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    session_id = "sess_v2_stale_redis"
    qid_old = "q_old_redis"
    qid_new = "q_new_mysql"

    # MySQL 中当前活跃问题是 q_new_mysql
    active_record_mysql = {
        "question_id": qid_new,
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "question_text": "MySQL 新问题",
            "options": [
                {"option_id": 1, "text": "新选项 1"},
            ],
        },
        "private_snapshot": {
            "option_modifications": {1: {"dish_count_requested": 3}},
            "query_plan_snapshot": {"dish_count_requested": 4},
        },
        "expires_at": 9999999999.0,
    }

    # Redis 中残留过期/旧问题
    redis_pending = [
        {
            "question_id": qid_old,
            "session_id": session_id,
            "status": "pending",
            "options": [
                {"option_id": 1, "text": "旧选项 1", "modifications": {"dish_count_requested": 1}},
            ],
            "expires_at": 1000.0,  # 已过期
        }
    ]

    mock_c4 = SimpleNamespace(
        build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=session_id), None),
        validate_context_integrity=lambda *args, **kwargs: {"valid": True},
        get_session_state=lambda sid: {"current_menu": {}},
        load_clarification_state=lambda sid: {
            "session_id": session_id,
            "active_question_id": qid_new,
            "clarification_revision": 2,
            "protocol_version": "v2",
        },
        load_active_clarification=lambda sid: active_record_mysql,
        is_clarification_committed=lambda sid, q: False,
        get_pending_clarifications=lambda sid: redis_pending,
        consume_pending_clarification=lambda *args, **kwargs: None,
    )

    state = {
        "request_id": str(uuid.uuid4()),
        "session_id": session_id,
        "build_id": str(uuid.uuid4()),
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": mock_c4,
        "message": "确认选项",
        # 尝试选择旧问题 -> 必须被拒绝
        "clarification_response": {"question_id": qid_old, "option_id": 1},
        "workflow_state": WorkflowState(
            request_id=str(uuid.uuid4()),
            build_id=str(uuid.uuid4()),
            status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        ),
        "tool_context": ToolContext(request_id=str(uuid.uuid4()), build_id=str(uuid.uuid4())),
    }

    res_stale = runner._node_understand_intent(state)
    assert res_stale.get("is_terminal") is True
    assert res_stale.get("error_code") == "CLARIFICATION_STALE"

    # 选择新问题 -> 必须成功并消费新问题
    state["clarification_response"] = {"question_id": qid_new, "option_id": 1}
    state["workflow_state"] = WorkflowState(
        request_id=str(uuid.uuid4()),
        build_id=str(uuid.uuid4()),
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    res_ok = runner._node_understand_intent(state)
    assert not res_ok.get("is_terminal")
    assert res_ok.get("pending_clarification_to_consume") == qid_new
    assert res_ok.get("clarification_transition").expected_revision == 2


def test_v2_mysql_query_exception_fails_explicitly_no_redis_fallback():
    """MySQL 查询异常时请求明确失败，不回退 Redis 猜测。"""
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    session_id = "sess_v2_mysql_err"

    def _broken_load_active(*args, **kwargs):
        raise ConnectionError("MySQL database unavailable")

    redis_pending = [
        {
            "question_id": "q_redis_guess",
            "session_id": session_id,
            "status": "pending",
            "options": [{"option_id": 1, "text": "Redis 猜测选项"}],
            "expires_at": 9999999999.0,
        }
    ]

    mock_c4 = SimpleNamespace(
        build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=session_id), None),
        validate_context_integrity=lambda *args, **kwargs: {"valid": True},
        get_session_state=lambda sid: {"current_menu": {}},
        load_clarification_state=lambda sid: {
            "session_id": session_id,
            "active_question_id": "q_any",
            "clarification_revision": 1,
            "protocol_version": "v2",
        },
        load_active_clarification=_broken_load_active,
        is_clarification_committed=lambda sid, q: False,
        get_pending_clarifications=lambda sid: redis_pending,
    )

    state = {
        "request_id": str(uuid.uuid4()),
        "session_id": session_id,
        "build_id": str(uuid.uuid4()),
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": mock_c4,
        "message": "回复",
        "clarification_response": {"question_id": "q_redis_guess", "option_id": 1},
        "workflow_state": WorkflowState(
            request_id=str(uuid.uuid4()),
            build_id=str(uuid.uuid4()),
            status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        ),
        "tool_context": ToolContext(request_id=str(uuid.uuid4()), build_id=str(uuid.uuid4())),
    }

    res = runner._node_understand_intent(state)
    assert res.get("is_terminal") is True
    # 明确失败，绝对不能降级成使用 Redis 猜测
    assert res.get("error_code") == "CLARIFICATION_LOOKUP_FAILED"


def test_v2_structured_response_applies_only_mysql_private_snapshot_modifications():
    """结构化 question_id/option_id 只应用 MySQL 的私有修改，不从 public payload 获取。"""
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    session_id = "sess_v2_priv_mods"
    qid = "q_mods_1"

    # public_payload 没有 modifications
    active_record = {
        "question_id": qid,
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "question_text": "请选择餐品调整",
            "options": [
                {"option_id": 1, "text": "减少到 2 个菜"},
            ],
        },
        # 私有 snapshot 包含权威修改映射
        "private_snapshot": {
            "option_modifications": {
                1: {"dish_count_requested": 2, "time_constraint_policy": "hard"},
            },
            "query_plan_snapshot": {
                "request_id": str(uuid.uuid4()),
                "dish_count_requested": 4,
                "meal_types": ["dinner"],
                "participant_refs": ["p1"],
            },
        },
        "expires_at": 9999999999.0,
    }

    mock_c4 = SimpleNamespace(
        build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=session_id), None),
        validate_context_integrity=lambda *args, **kwargs: {"valid": True},
        get_session_state=lambda sid: {"current_menu": {}},
        load_clarification_state=lambda sid: {
            "session_id": session_id,
            "active_question_id": qid,
            "clarification_revision": 3,
            "protocol_version": "v2",
        },
        load_active_clarification=lambda sid: active_record,
        is_clarification_committed=lambda sid, q: False,
        get_pending_clarifications=lambda sid: [],
    )

    state = {
        "request_id": str(uuid.uuid4()),
        "session_id": session_id,
        "build_id": str(uuid.uuid4()),
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": mock_c4,
        "message": "确认",
        "clarification_response": {"question_id": qid, "option_id": 1},
        "workflow_state": WorkflowState(
            request_id=str(uuid.uuid4()),
            build_id=str(uuid.uuid4()),
            status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        ),
        "tool_context": ToolContext(request_id=str(uuid.uuid4()), build_id=str(uuid.uuid4())),
    }

    res = runner._node_understand_intent(state)
    assert not res.get("is_terminal")
    qp = res.get("query_plan")
    assert qp is not None
    # 成功从 private_snapshot 应用 dish_count_requested=2
    assert qp.dish_count_requested == 2
    trans = res.get("clarification_transition")
    assert trans is not None
    assert trans.expected_revision == 3


def test_lost_lock_needs_clarification_does_not_commit_or_write_question():
    """失锁后 needs_clarification 不写新问题，不执行 commit_request_result。"""
    from food_agent_v2.c3.runtime import AgentRuntime
    runner = AgentRuntime()
    rid = str(uuid.uuid4())
    session_id = "sess_lost_lock_nc"

    wf_state = WorkflowState(
        request_id=rid,
        build_id=str(uuid.uuid4()),
        status=RequestStatus.NEEDS_CLARIFICATION,
        shared_context_ref=session_id,
    )

    commit_mock = MagicMock()
    mock_c4 = SimpleNamespace(
        is_session_lock_held=lambda sid, token: False,  # 锁已丢失！
        commit_session_state=MagicMock(),
    )

    with patch("food_agent_v2.application.commit_request_result", commit_mock), \
         patch("food_agent_v2.d1.api.update_status"):
        final_status = runner._finalize(
            wf_state, rid, mock_c4, lock_token="token-lost",
            clarification_transition=SimpleNamespace(next_question_id="q_new")
        )

    # 终态应变为 failed（SESSION_LOCK_LOST）
    assert final_status == "failed"
    # commit_request_result 绝对不能被调用（不写新问题）
    assert commit_mock.call_count == 0


def test_v2_plain_text_option_reply_rejected_by_session_protocol():
    """纯文本'选第一个'在 v2 会话下拒绝（由 MySQL protocol_version='v2' 判定，无需环境变量）。"""
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    session_id = "sess_v2_plain_reject"
    qid = "q_active_reject"

    active_record = {
        "question_id": qid,
        "session_id": session_id,
        "status": "pending",
        "public_payload": {
            "options": [
                {"option_id": 1, "text": "方案 1"},
                {"option_id": 2, "text": "方案 2"},
            ]
        },
        "expires_at": 9999999999.0,
    }

    mock_c4 = SimpleNamespace(
        build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=session_id), None),
        validate_context_integrity=lambda *args, **kwargs: {"valid": True},
        get_session_state=lambda sid: {"current_menu": {}},
        load_clarification_state=lambda sid: {
            "session_id": session_id,
            "active_question_id": qid,
            "clarification_revision": 1,
            "protocol_version": "v2",  # 持久化标记为 v2
        },
        load_active_clarification=lambda sid: active_record,
        is_clarification_committed=lambda sid, q: False,
        get_pending_clarifications=lambda sid: [],
    )

    state = {
        "request_id": str(uuid.uuid4()),
        "session_id": session_id,
        "build_id": str(uuid.uuid4()),
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "lock_token": "token-test",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": mock_c4,
        "message": "选第一个",  # 纯文本回复
        "clarification_response": None,
        "workflow_state": WorkflowState(
            request_id=str(uuid.uuid4()),
            build_id=str(uuid.uuid4()),
            status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        ),
        "tool_context": ToolContext(request_id=str(uuid.uuid4()), build_id=str(uuid.uuid4())),
    }

    res = runner._node_understand_intent(state)
    assert res.get("is_terminal") is True
    assert res.get("error_code") == "CLARIFICATION_REFERENCE_REQUIRED"


def test_register_active_fencing_token_semantics():
    """测试 active_fencing_token 比较登记与失锁 fail-closed 逻辑。"""
    from food_agent_v2.c4.mysql_repository import InMemorySessionMemorySource
    source = InMemorySessionMemorySource()
    sid = "sess_fence_test"

    # 首次登记 token=10 -> 成功
    assert source.register_active_fencing_token(sid, 10) is True
    assert source.load_clarification_state(sid)["active_fencing_token"] == 10

    # 重复/更小 token=10, token=9 -> 拒绝
    assert source.register_active_fencing_token(sid, 10) is False
    assert source.register_active_fencing_token(sid, 9) is False

    # 更大 token=11 -> 成功
    assert source.register_active_fencing_token(sid, 11) is True
    assert source.load_clarification_state(sid)["active_fencing_token"] == 11


def test_resolve_option_selection_strict_matching_and_negative_guard():
    """测试 Task 2：收紧文字选项识别，区分明确选项表达与全新业务意图。"""
    opts = [
        {"option_id": 1, "text": "1道菜"},
        {"option_id": 2, "text": "2道菜"},
        {"option_id": 3, "text": "放宽制作时间"},
    ]

    # 1. 明确正向选项表达 -> matched
    res, status = LangGraphRecommendationOrchestrator._resolve_option_selection("选第一个", opts)
    assert status == "matched"
    assert res["option_id"] == 1

    res, status = LangGraphRecommendationOrchestrator._resolve_option_selection("按第2个来", opts)
    assert status == "matched"
    assert res["option_id"] == 2

    res, status = LangGraphRecommendationOrchestrator._resolve_option_selection("2", opts)
    assert status == "matched"
    assert res["option_id"] == 2

    res, status = LangGraphRecommendationOrchestrator._resolve_option_selection("二", opts)
    assert status == "matched"
    assert res["option_id"] == 2

    res, status = LangGraphRecommendationOrchestrator._resolve_option_selection("选项3", opts)
    assert status == "matched"
    assert res["option_id"] == 3

    # 完全等于选项文本
    res, status = LangGraphRecommendationOrchestrator._resolve_option_selection("2道菜", opts)
    assert status == "matched"
    assert res["option_id"] == 2

    res, status = LangGraphRecommendationOrchestrator._resolve_option_selection("放宽制作时间。", opts)
    assert status == "matched"
    assert res["option_id"] == 3

    # 2. 负向全新意图表达 -> not_an_option（绝不能误判为选项确认）
    _, status = LangGraphRecommendationOrchestrator._resolve_option_selection("推荐一道清淡家常菜", opts)
    assert status == "not_an_option"

    _, status = LangGraphRecommendationOrchestrator._resolve_option_selection("换成三道菜", opts)
    assert status == "not_an_option"

    _, status = LangGraphRecommendationOrchestrator._resolve_option_selection("重新推荐", opts)
    assert status == "not_an_option"

    _, status = LangGraphRecommendationOrchestrator._resolve_option_selection("不放宽时间，重新做", opts)
    assert status == "not_an_option"

    _, status = LangGraphRecommendationOrchestrator._resolve_option_selection("不要葱蒜", opts)
    assert status == "not_an_option"

    # 3. 歧义词排查
    _, status = LangGraphRecommendationOrchestrator._resolve_option_selection("随便选一个", opts)
    assert status == "ambiguous"

    # 4. 越界
    _, status = LangGraphRecommendationOrchestrator._resolve_option_selection("选第9个", opts)
    assert status == "out_of_range"
