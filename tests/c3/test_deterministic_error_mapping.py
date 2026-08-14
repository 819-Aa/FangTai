"""确定性链路工具错误映射与稳定选优测试（L1 Task 7）。"""

from __future__ import annotations

from types import SimpleNamespace

from food_agent_v2.c3.state import WorkflowState


def _select_best(plans):
    # 与 orchestrator 内联相同的稳定选优：(-total_score, plan_id) tie-break
    return sorted(
        plans, key=lambda p: (-getattr(p, "total_score", 0.0),
                              getattr(p, "plan_id", "")))[0]


def test_equal_scores_use_stable_plan_id_tiebreak():
    plans = [
        SimpleNamespace(total_score=1.0, plan_id="b"),
        SimpleNamespace(total_score=1.0, plan_id="a"),
    ]
    assert _select_best(plans).plan_id == "a"


def test_higher_score_wins_regardless_of_plan_id():
    plans = [
        SimpleNamespace(total_score=0.5, plan_id="a"),
        SimpleNamespace(total_score=2.0, plan_id="z"),
    ]
    assert _select_best(plans).plan_id == "z"


def test_tool_error_fail_maps_to_failed_not_business_terminal():
    # 工具返回 error（非业务 note）→ fail action 置 failed（TOOL_EXECUTION_FAILED），
    # 而非 business terminal（no_feasible_menu）
    from food_agent_v2.c3.state import (
        RequestStatus,
        WorkflowError,
        reduce_workflow_state,
    )
    state = WorkflowState(request_id="r", build_id="b")
    result = reduce_workflow_state(
        state, action="fail",
        error=WorkflowError("TOOL_EXECUTION_FAILED", "db unavailable"))
    assert result.status == RequestStatus.FAILED
    assert result.error.error_code == "TOOL_EXECUTION_FAILED"
