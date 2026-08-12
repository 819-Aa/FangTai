"""MC-03：已提交结果投影与 outbox 传输故障边界。"""

from unittest.mock import patch
from uuid import UUID

from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.state import RequestStatus, WorkflowState
from food_agent_v2.contracts.artifacts import (
    AnswerArtifact,
    FinalValidationArtifact,
    HealthEvaluationArtifact,
    MenuDecisionArtifact,
    ReviewArtifact,
)
from food_agent_v2.d1 import api as d1_api

RID = "11111111-1111-1111-1111-111111111111"
SID = "sess_mc03"
PLAN = "plan-A"
MENU_HASH = "a" * 64


class _C4:
    def commit_session_state(self, request_id, status, **kwargs):
        return None


def _completed_state() -> WorkflowState:
    rid = UUID(RID)
    state = WorkflowState(
        request_id=RID,
        status=RequestStatus.COMPLETED,
        shared_context_ref=SID,
        participant_refs=["p1"],
        build_id="7" * 32,
    )
    state.health_evaluation_artifact = HealthEvaluationArtifact(
        artifact_id=UUID("10000000-0000-0000-0000-000000000001"),
        request_id=rid,
        participant_refs=("p1",),
        constraint_set_refs=("constraint:p1",),
        participant_recipe_results=(),
        recipe_group_results=(),
        safe_recipe_ids=(101, 202),
        excluded_recipe_ids=(),
        input_fingerprint="1" * 64,
        content_hash="2" * 64,
    )
    state.menu_decision_artifact = MenuDecisionArtifact(
        artifact_id=UUID("20000000-0000-0000-0000-000000000001"),
        request_id=rid,
        plan_id=PLAN,
        recipe_ids=(101, 202),
        menu_hash=MENU_HASH,
        feasible_menu_artifact_ref="feasible:1",
        final_validation_ref="30000000-0000-0000-0000-000000000001",
        participant_refs=("p1",),
        evidence_refs=("decision:evidence",),
        content_hash="3" * 64,
    )
    state.final_validation_artifact = FinalValidationArtifact(
        artifact_id=UUID("30000000-0000-0000-0000-000000000001"),
        request_id=rid,
        plan_id=PLAN,
        menu_artifact_ref="feasible:1",
        participant_refs=("p1",),
        recipe_ids=(101, 202),
        participant_recipe_results=(),
        relation_evidence_refs=("relation:1",),
        menu_hash=MENU_HASH,
        input_fingerprint="4" * 64,
        status="PASS",
    )
    state.answer_artifact = AnswerArtifact(
        artifact_id=UUID("40000000-0000-0000-0000-000000000001"),
        request_id=rid,
        plan_id=PLAN,
        menu_ref="feasible:1",
        final_validation_ref="30000000-0000-0000-0000-000000000001",
        recipe_ids=(101, 202),
        menu_hash=MENU_HASH,
        content={
            "conclusion": "推荐两道家常菜。",
            "menu_summary": "清爽搭配，适合日常用餐。",
        },
        evidence_refs=("answer:evidence",),
        content_hash="5" * 64,
    )
    state.review_artifact = ReviewArtifact(
        artifact_id=UUID("50000000-0000-0000-0000-000000000001"),
        request_id=rid,
        status="PASS",
        content_hash="6" * 64,
    )
    return state


def _seed_request() -> None:
    d1_api._requests[RID] = {
        "request_id": RID,
        "session_id": SID,
        "status": "running",
        "created_at": "t",
        "stage_events_cursor": 0,
    }
    d1_api._events[RID] = []
    d1_api._event_cursors[RID] = 1


def test_committed_result_survives_immediate_dispatch_failure() -> None:
    """业务事务已成功时，SSE 即时投递失败不得把 completed 反写为 failed。"""
    _seed_request()
    state = _completed_state()
    runner = WorkflowRunner(build_id="7" * 32, llm=object(), c4=_C4())

    with patch.object(d1_api, "_persist_request", lambda request_id: None), \
            patch("food_agent_v2.application.commit_request_result",
                  return_value={"committed": True}), \
            patch("food_agent_v2.application.menu_projection.build_public_menu",
                  return_value=[
                      {"recipe_id": 101, "name": "菜品甲"},
                      {"recipe_id": 202, "name": "菜品乙"},
                  ]), \
            patch("food_agent_v2.application.outbox.dispatch_request",
                  side_effect=RuntimeError("redis temporarily unavailable")):
        runner._finalize(state, RID, _C4(), lock_token="1")

    code, status = d1_api.get_request_status(RID)
    assert code == 200
    assert status["status"] == "completed"
    assert status["error"] is None
    assert status["result_summary"] == {
        "status": "completed",
        "answer": {
            "text": "推荐两道家常菜。\n清爽搭配，适合日常用餐。",
            "menu_ref": "feasible:1",
            "evidence_refs": ["answer:evidence"],
        },
        "menu_summary": {
            "build_id": "7" * 32,
            "plan_id": PLAN,
            "menu_hash": MENU_HASH,
            "recipe_ids": [101, 202],
            "items": [
                {"recipe_id": 101, "name": "菜品甲"},
                {"recipe_id": 202, "name": "菜品乙"},
            ],
        },
    }
    assert not any(e["event"] == "request_terminal"
                   for e in d1_api.subscribe_events(RID))


def test_commit_failure_has_no_success_projection() -> None:
    """Application 提交失败仍 fail-closed，不能留下可被轮询读取的成功菜单。"""
    _seed_request()
    state = _completed_state()
    runner = WorkflowRunner(build_id="7" * 32, llm=object(), c4=_C4())

    with patch.object(d1_api, "_persist_request", lambda request_id: None), \
            patch("food_agent_v2.application.commit_request_result",
                  side_effect=RuntimeError("commit failed")), \
            patch("food_agent_v2.application.menu_projection.build_public_menu",
                  return_value=[
                      {"recipe_id": 101, "name": "菜品甲"},
                      {"recipe_id": 202, "name": "菜品乙"},
                  ]), \
            patch("food_agent_v2.application.outbox.dispatch_request") as dispatch:
        runner._finalize(state, RID, _C4(), lock_token="1")

    code, status = d1_api.get_request_status(RID)
    assert code == 200
    assert status["status"] == "failed"
    assert status["error"]["code"] == "AUDIT_COMMIT_FAILED"
    assert status["result_summary"] == {"status": "failed"}
    dispatch.assert_not_called()
