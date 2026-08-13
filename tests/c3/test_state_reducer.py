"""T16 C3 纯 State Reducer 测试。

所有状态更新经 reduce_workflow_state（纯函数，返回新状态，不改原 state）；
模型不能写 state；循环计数器与终态判定正确。
"""

from food_agent_v2.c3.state import (
    NodeType,
    RequestStatus,
    WorkflowState,
    can_model_write_state,
    reduce_workflow_state,
)


def make_state() -> WorkflowState:
    return WorkflowState(request_id="r1")


class TestPureReducer:
    def test_reducer_is_pure(self) -> None:
        state = make_state()
        new_state = reduce_workflow_state(state, action="query_understanding", success=True)
        assert new_state.current_node == NodeType.HEALTH_MENU_PLANNING
        assert state.current_node is None  # 原 state 未被修改
        assert new_state is not state

    def test_query_clarification(self) -> None:
        new_state = reduce_workflow_state(
            make_state(), action="query_understanding", success=True, needs_clarification=True
        )
        assert new_state.status == RequestStatus.NEEDS_CLARIFICATION

    def test_query_failure(self) -> None:
        new_state = reduce_workflow_state(make_state(), action="query_understanding", success=False)
        assert new_state.status == RequestStatus.FAILED

    def test_no_safe_menu(self) -> None:
        new_state = reduce_workflow_state(make_state(), action="health_menu_planning", result="no_safe_menu")
        assert new_state.status == RequestStatus.NO_SAFE_MENU

    def test_no_feasible_menu(self) -> None:
        new_state = reduce_workflow_state(make_state(), action="health_menu_planning", result="no_feasible_menu")
        assert new_state.status == RequestStatus.NO_FEASIBLE_MENU

    def test_strict_time_indeterminate(self) -> None:
        new_state = reduce_workflow_state(
            make_state(), action="health_menu_planning", result="strict_time_indeterminate")
        assert new_state.status == RequestStatus.STRICT_TIME_INDETERMINATE
        assert new_state.current_node == NodeType.ATOMIC_COMMIT
        assert new_state.is_terminal()

    def test_expansion_counter_and_limit(self) -> None:
        state = make_state()
        first = reduce_workflow_state(state, action="health_menu_planning", result="needs_expansion")
        assert first.status == RequestStatus.REVISING
        assert first.retrieval_expansion_count == 1
        second = reduce_workflow_state(first, action="health_menu_planning", result="needs_expansion")
        assert second.status == RequestStatus.FAILED
        assert second.error is not None
        assert second.error.error_code == "WORKFLOW_RETRY_LIMIT_EXCEEDED"

    def test_final_validation_failure(self) -> None:
        new_state = reduce_workflow_state(make_state(), action="menu_decision", validation_pass=False)
        assert new_state.status == RequestStatus.FAILED
        assert new_state.error is not None
        assert new_state.error.error_code == "FINAL_HEALTH_VALIDATION_FAILED"

    def test_completed(self) -> None:
        new_state = reduce_workflow_state(make_state(), action="unified_review", status="PASS")
        assert new_state.status == RequestStatus.COMPLETED
        assert new_state.is_terminal()

    def test_unknown_action_fails(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            reduce_workflow_state(make_state(), action="unknown")

    def test_model_cannot_write_state(self) -> None:
        assert can_model_write_state() is False
