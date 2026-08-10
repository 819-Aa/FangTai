"""T17 C3 有界状态机转换测试。

验证纯 reducer 的完整转换图、固定循环预算（各最多 1 次）、未知 verdict/结果
fail-closed，以及 runner 支持动作（set_node/set_context_ref/set_artifact/
record_receipts/set_status/fail）。
"""

from uuid import UUID

import pytest

from food_agent_v2.c3 import WorkflowError
from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.state import NodeType, RequestStatus, WorkflowState, reduce_workflow_state
from food_agent_v2.contracts.receipts import ToolReceipt

RID = UUID("11111111-1111-1111-1111-111111111111")
BID = UUID("22222222-2222-2222-2222-222222222222")


def make_state(**overrides) -> WorkflowState:
    state = WorkflowState(request_id="r1", build_id=str(BID))
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def make_receipt() -> ToolReceipt:
    return ToolReceipt(
        request_id=RID,
        node_id="query_understanding",
        tool_call_id="call-1",
        tool_name="retrieve_recipes",
        input_hash="a" * 64,
        output_hash="b" * 64,
        build_id=BID,
        success=True,
    )


class TestTransitionGraph:
    def test_context_building_success(self) -> None:
        s = reduce_workflow_state(make_state(), action="context_building", manifest_valid=True)
        assert s.current_node == NodeType.QUERY_UNDERSTANDING
        assert s.status == RequestStatus.ACCEPTED  # 转换不改变非终态 status

    def test_context_building_failure(self) -> None:
        s = reduce_workflow_state(make_state(), action="context_building", manifest_valid=False)
        assert s.status == RequestStatus.FAILED
        assert s.error is not None
        assert s.error.error_code == "CONTEXT_INTEGRITY_FAILED"

    def test_query_clarification_terminal(self) -> None:
        s = reduce_workflow_state(make_state(), action="query_understanding",
                                  success=True, needs_clarification=True)
        assert s.status == RequestStatus.NEEDS_CLARIFICATION
        assert s.is_terminal()

    def test_query_failure(self) -> None:
        s = reduce_workflow_state(make_state(), action="query_understanding", success=False)
        assert s.status == RequestStatus.FAILED

    def test_health_planning_ok(self) -> None:
        s = reduce_workflow_state(make_state(), action="health_menu_planning", result="ok")
        assert s.current_node == NodeType.MENU_DECISION

    def test_no_safe_menu_terminal(self) -> None:
        s = reduce_workflow_state(make_state(), action="health_menu_planning", result="no_safe_menu")
        assert s.status == RequestStatus.NO_SAFE_MENU

    def test_menu_decision_pass(self) -> None:
        s = reduce_workflow_state(make_state(), action="menu_decision", validation_pass=True)
        assert s.current_node == NodeType.ANSWER_GENERATION

    def test_answer_generation(self) -> None:
        s = reduce_workflow_state(make_state(), action="answer_generation", success=True)
        assert s.current_node == NodeType.UNIFIED_REVIEW

    def test_review_pass_completes(self) -> None:
        s = reduce_workflow_state(make_state(), action="unified_review", verdict="PASS")
        assert s.status == RequestStatus.COMPLETED
        assert s.current_node == NodeType.ATOMIC_COMMIT
        assert s.is_terminal()


class TestUnknownVerdictFailClosed:
    def test_unknown_review_verdict_fails(self) -> None:
        for verdict in ("REVIEW_REQUIRED", "weird", "", None):
            s = reduce_workflow_state(make_state(), action="unified_review", verdict=verdict)
            assert s.status == RequestStatus.FAILED, f"verdict={verdict!r} 应 fail-closed"

    def test_unknown_health_result_fails(self) -> None:
        s = reduce_workflow_state(make_state(), action="health_menu_planning", result="bogus")
        assert s.status == RequestStatus.FAILED

    def test_unknown_action_raises(self) -> None:
        with pytest.raises(ValueError):
            reduce_workflow_state(make_state(), action="nope")


class TestBoundedBudgets:
    def test_retrieval_expansion_budget(self) -> None:
        s = make_state()
        s = reduce_workflow_state(s, action="health_menu_planning", result="needs_expansion")
        assert s.status == RequestStatus.REVISING
        assert s.retrieval_expansion_count == 1
        s = reduce_workflow_state(s, action="health_menu_planning", result="needs_expansion")
        assert s.status == RequestStatus.FAILED
        assert s.error is not None
        assert s.error.error_code == "WORKFLOW_RETRY_LIMIT_EXCEEDED"

    def test_health_replan_budget(self) -> None:
        s = make_state()
        s = reduce_workflow_state(s, action="menu_decision", needs_replan=True)
        assert s.status == RequestStatus.REVISING
        assert s.current_node == NodeType.HEALTH_MENU_PLANNING
        s = reduce_workflow_state(s, action="menu_decision", needs_replan=True)
        assert s.status == RequestStatus.FAILED
        assert s.error.error_code == "WORKFLOW_RETRY_LIMIT_EXCEEDED"

    def test_review_revision_budget(self) -> None:
        s = make_state()
        s = reduce_workflow_state(s, action="unified_review", verdict="REVISION_REQUIRED")
        assert s.status == RequestStatus.REVISING
        assert s.current_node == NodeType.ANSWER_GENERATION
        assert s.review_revision_count == 1
        s = reduce_workflow_state(s, action="unified_review", verdict="REVISION_REQUIRED")
        assert s.status == RequestStatus.FAILED
        assert s.error.error_code == "WORKFLOW_RETRY_LIMIT_EXCEEDED"


class TestRunnerSupportActions:
    def test_set_node(self) -> None:
        s = reduce_workflow_state(make_state(), action="set_node", node=NodeType.CONTEXT_BUILDING)
        assert s.current_node == NodeType.CONTEXT_BUILDING

    def test_set_context_ref(self) -> None:
        s = reduce_workflow_state(make_state(), action="set_context_ref", shared_context_ref="sess-1")
        assert s.shared_context_ref == "sess-1"

    def test_set_artifact(self) -> None:
        s = reduce_workflow_state(make_state(), action="set_artifact",
                                  artifact="query_plan", value={"k": 1})
        assert s.query_plan_artifact == {"k": 1}

    def test_unknown_artifact_raises(self) -> None:
        with pytest.raises(ValueError):
            reduce_workflow_state(make_state(), action="set_artifact", artifact="bogus", value=1)

    def test_record_receipts(self) -> None:
        s = make_state()
        s = reduce_workflow_state(s, action="record_receipts", receipts=[make_receipt()])
        assert len(s.tool_receipts) == 1
        assert s.tool_receipts[0].tool_name == "retrieve_recipes"

    def test_set_status_cancelled(self) -> None:
        s = reduce_workflow_state(make_state(), action="set_status", status=RequestStatus.CANCELLED)
        assert s.status == RequestStatus.CANCELLED
        assert s.is_terminal()

    def test_fail(self) -> None:
        s = make_state(current_node=NodeType.QUERY_UNDERSTANDING)
        s = reduce_workflow_state(s, action="fail", error=WorkflowError("X", "msg"))
        assert s.status == RequestStatus.FAILED
        assert s.error is not None
        assert s.error.error_code == "X"

    def test_reducer_is_pure_for_support_actions(self) -> None:
        s = make_state()
        new = reduce_workflow_state(s, action="set_artifact", artifact="answer", value={"c": 1})
        assert s.answer_artifact is None  # 原 state 未修改
        assert new.answer_artifact == {"c": 1}


class TestRunnerHelpers:
    def test_menu_hash_deterministic_order_independent(self) -> None:
        assert WorkflowRunner._menu_hash([3, 1, 2]) == WorkflowRunner._menu_hash([1, 2, 3])
        assert len(WorkflowRunner._menu_hash([1, 2])) == 64

    def test_tool_budget_duplicate_detected(self) -> None:
        dup = [{"tool_name": "retrieve_recipes", "input_hash": "a" * 64}] * 2
        err = WorkflowRunner._validate_tool_budget(dup)
        assert err is not None
        assert err.error_code == "WORKFLOW_RETRY_LIMIT_EXCEEDED"
        distinct = [
            {"tool_name": "retrieve_recipes", "input_hash": "a" * 64},
            {"tool_name": "get_current_menu", "input_hash": "b" * 64},
        ]
        assert WorkflowRunner._validate_tool_budget(distinct) is None
