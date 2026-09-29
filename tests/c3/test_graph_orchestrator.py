"""C3 LangGraph 混合编排器与 Agent 工具集单元测试。

验证核心契约：
1. 零静默放宽（Zero Silent Relaxation）：时间超限、菜数不足、无解时绝不自动放宽；
2. 归因诊断（Diagnostics）：精确输出超时分钟数、差额菜数与具体禁忌原因；
3. 协商追问（Human-in-the-Loop）：生成详尽解释与 2~3 个结构化选项，进入 needs_clarification；
4. LangGraph 状态图拓扑与条件边流转正确无误。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

from food_agent_v2.c2.schemas import FeasibleMenu, MenuHardConstraints, menu_hash_for
from food_agent_v2.c3.agent_actions import ActionType, AgentAction, Observation
from food_agent_v2.c3.agent_policy import AgentPolicy
from food_agent_v2.c3.fast_intent import IntentDelta
from food_agent_v2.c3.graph_orchestrator import (
    DeterministicScriptedAgentModel,
    DietAgentState,
    LangGraphRecommendationOrchestrator,
)
from food_agent_v2.c3.state import RequestStatus, WorkflowState, reduce_workflow_state
from food_agent_v2.c3.tool_handler import ToolContext, ToolHandler
from food_agent_v2.c3.tools import (
    ToolResponse,
    combine_nutritional_menu,
    search_candidates,
)
from food_agent_v2.contracts.artifacts import (
    FinalValidationArtifact,
    QueryPlanArtifact,
)
from food_agent_v2.d1 import api as d1_api


def _fresh_rid() -> str:
    return str(uuid.uuid4())


_REAL_TOOL_EXECUTE = ToolHandler.execute


def _validation_tool_result(fv):
    return {
        "verdict": fv.status,
        "plan_id": fv.plan_id,
        "recipe_ids": list(fv.recipe_ids),
        "menu_hash": fv.menu_hash,
        "final_validation_ref": str(fv.artifact_id),
    }


def _execute_with_bound_final_validation(handler, tool_name, arguments):
    """Supply explicit test evidence bound to the requested feasible menu."""
    if tool_name != "validate_selected_menu_health":
        return _REAL_TOOL_EXECUTE(handler, tool_name, arguments)
    ctx = handler._ctx
    plan = next(
        plan for plan in ctx.previous_results["feasible_menus"]
        if plan.plan_id == arguments["plan_id"]
    )
    assert list(plan.recipe_ids) == arguments["recipe_ids"]
    assert plan.menu_hash == menu_hash_for(plan.plan_id, list(plan.recipe_ids))
    qp = ctx.previous_results["query_plan"]
    fv = FinalValidationArtifact(
        artifact_id=uuid.uuid4(),
        request_id=UUID(ctx.request_id),
        plan_id=plan.plan_id,
        menu_artifact_ref="test-feasible-menu",
        participant_refs=qp.participant_refs,
        recipe_ids=tuple(plan.recipe_ids),
        participant_recipe_results=(),
        menu_hash=plan.menu_hash,
        input_fingerprint="b" * 64,
        status="PASS",
    )
    ctx.previous_results["final_validation"] = fv
    return _validation_tool_result(fv)


# ============================================================================
# 1. c3/tools.py 单元测试
# ============================================================================


def test_search_candidates_empty_returns_candidates_required() -> None:
    mock_svc = MagicMock()
    mock_svc.retrieve.return_value = SimpleNamespace(candidates=[], total_candidates=0)

    with patch("food_agent_v2.c3.tools.get_retrieval_service", return_value=mock_svc):
        res = search_candidates(query="不存在的菜品")
        assert res.success is False
        assert res.error_code == "CANDIDATES_REQUIRED"
        assert res.diagnostics.get("total_candidates") == 0


def test_search_candidates_success() -> None:
    mock_candidates = [
        SimpleNamespace(recipe_id=1, name="番茄鸡蛋汤"),
        SimpleNamespace(recipe_id=2, name="青椒肉丝"),
    ]
    mock_svc = MagicMock()
    mock_svc.retrieve.return_value = SimpleNamespace(
        candidates=mock_candidates, total_candidates=2
    )

    with patch("food_agent_v2.c3.tools.get_retrieval_service", return_value=mock_svc):
        res = search_candidates(query="家常菜")
        assert res.success is True
        assert res.data["candidates"] == [1, 2]
        assert res.data["total"] == 2


def test_combine_nutritional_menu_safe_candidate_shortage() -> None:
    """安全候选少于目标菜数：零静默放宽，返回精确差额诊断。"""
    safe_ids = [1, 2]
    dish_count = 4

    res = combine_nutritional_menu(
        safe_recipe_ids=safe_ids,
        dish_count=dish_count,
    )

    assert res.success is False
    assert res.error_code == "SAFE_CANDIDATE_SHORTAGE"
    assert res.diagnostics["bottleneck"] == "SAFE_CANDIDATE_SHORTAGE"
    assert res.diagnostics["safe_count"] == 2
    assert res.diagnostics["requested_count"] == 4
    assert res.diagnostics["deficit"] == 2


def test_combine_nutritional_menu_time_limit_exceeded_diagnosis() -> None:
    """烹饪时间超限：诊断最快组合所需时间与请求上限。"""
    safe_ids = [1, 2, 3, 4, 5]
    dish_count = 3
    time_limit_seconds = 900  # 15 分钟

    def mock_plan(constraints: MenuHardConstraints, *args, **kwargs):
        if constraints.max_estimated_time_seconds is not None:
            # 严格时限下无法达成
            return []
        # 无时限约束下，最快方案需要 1800 秒（30 分钟）
        return [
            FeasibleMenu(
                plan_id="plan-1",
                recipe_ids=[1, 2, 3],
                menu_hash=menu_hash_for("plan-1", [1, 2, 3]),
                dominant_objective="quick",
                total_score=85.0,
                time_score=0.8,
                nutrition_score=0.8,
                preference_score=0.8,
                diversity_score=0.8,
                estimated_time_feasible=True,
                estimated_makespan_seconds=1800,
            )
        ]

    with patch("food_agent_v2.c3.tools.MenuPlanner.plan", side_effect=mock_plan):
        res = combine_nutritional_menu(
            safe_recipe_ids=safe_ids,
            dish_count=dish_count,
            time_limit_seconds=time_limit_seconds,
        )

        assert res.success is False
        assert res.error_code == "TIME_LIMIT_EXCEEDED"
        assert res.diagnostics["bottleneck"] == "TIME_LIMIT_EXCEEDED"
        assert res.diagnostics["requested_time_minutes"] == 15
        assert res.diagnostics["min_needed_minutes"] == 30
        assert res.diagnostics["requested_dish_count"] == 3


def test_combine_nutritional_menu_no_feasible_menu_diagnosis() -> None:
    """结构性/组合不可行：全无解诊断。"""
    safe_ids = [1, 2, 3, 4]
    dish_count = 3

    with patch("food_agent_v2.c3.tools.MenuPlanner.plan", return_value=[]):
        res = combine_nutritional_menu(
            safe_recipe_ids=safe_ids,
            dish_count=dish_count,
            time_limit_seconds=1800,
        )

        assert res.success is False
        assert res.error_code == "NO_FEASIBLE_MENU"
        assert res.diagnostics["bottleneck"] == "STRUCTURAL_OR_COMBINATORIAL_UNSATISFIABLE"


# ============================================================================
# 2. c3/graph_orchestrator.py 单元与路由测试
# ============================================================================


def test_graph_orchestrator_initialization_and_compilation() -> None:
    """验证 LangGraph 编排器图构建与节点连接。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    assert orchestrator._graph is not None

    # 路由条件函数校验
    assert orchestrator._route_after_intent({"is_terminal": True}) == "error"
    assert orchestrator._route_after_intent({"inquiry_needed": True}) == "inquire"
    assert orchestrator._route_after_intent({"inquiry_needed": False}) == "decide"

    # Agent 决策与执行循环路由校验
    assert orchestrator._route_after_decide({"is_terminal": True}) == "error"
    assert (
        orchestrator._route_after_decide(
            {"current_action": AgentAction(action=ActionType.ASK_USER)}
        )
        == "inquire"
    )
    assert (
        orchestrator._route_after_decide(
            {
                "current_action": AgentAction(
                    action=ActionType.SEARCH_CANDIDATES, arguments={"query": "test"}
                )
            }
        )
        == "execute"
    )
    assert orchestrator._route_after_execute({"is_terminal": True}) == "error"
    assert orchestrator._route_after_execute({"is_terminal": False}) == "decide"

    assert orchestrator._route_after_search({"inquiry_needed": True}) == "inquire"
    assert orchestrator._route_after_search({"inquiry_needed": False}) == "audit"

    assert orchestrator._route_after_audit({"inquiry_needed": True}) == "inquire"
    assert orchestrator._route_after_audit({"inquiry_needed": False}) == "combine"

    assert orchestrator._route_after_combine({"inquiry_needed": True}) == "inquire"
    assert orchestrator._route_after_combine({"inquiry_needed": False}) == "validate"


def test_node_inquire_user_generates_truthful_reason_and_options() -> None:
    """验证协商追问节点：精准归因、给出结构化选项、发布 SSE 事件并置为 needs_clarification。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())

    rid = _fresh_rid()
    wf_state = WorkflowState(
        request_id=rid,
        build_id="build-test",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )

    state: DietAgentState = {
        "request_id": rid,
        "workflow_state": wf_state,
        "diagnosis_code": "TIME_LIMIT_EXCEEDED",
        "inquiry_reason": (
            "您要求在 15 分钟内完成 4 道菜。现有最快安全组合预计需要约 28 分钟，"
            "因此无法在不超时的情况下凑齐 4 道菜。"
        ),
        "inquiry_options": [
            "选项 1: 将时间限制放宽至 28 分钟或以上，保留 4 道菜",
            "选项 2: 调整菜品数量为 2~3 道菜，以缩短烹饪制作耗时",
            "选项 3: 取消严格时间限制，优先保证营养均衡与菜品丰富度",
        ],
    }

    published_events = []
    with patch(
        "food_agent_v2.d1.api.publish_clarification_event",
        side_effect=lambda r, query_plan=None: published_events.append((r, query_plan)),
    ):
        result = orchestrator._node_inquire_user(state)

    updated_wf: WorkflowState = result["workflow_state"]
    assert updated_wf.status == RequestStatus.NEEDS_CLARIFICATION
    assert updated_wf.error is not None
    assert updated_wf.error.error_code == "TIME_LIMIT_EXCEEDED"
    assert "选项 1: 将时间限制放宽至 28 分钟或以上" in updated_wf.error.message
    assert result["is_terminal"] is True
    # 依据 2026-09-27 澄清生命周期规范（Invariant I2 Commit-Before-Publish）：
    # _node_inquire_user 暂存澄清状态转移声明，禁止在 MySQL 提交前向 SSE 提前发布交互事件
    assert len(published_events) == 0
    assert result.get("clarification_transition") is not None
    trans = result["clarification_transition"]
    assert trans.next_question_id == f"q_{rid}"
    assert "28 分钟" in trans.next_public_payload["question_text"]
    assert len(trans.next_public_payload["options"]) == 3


def test_node_combine_menu_handles_zero_silent_relaxation_for_time() -> None:
    """规划节点遇到超时：不自动放宽，转为 inquiry_needed 并计算诊断。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())

    rid = _fresh_rid()
    wf_state = WorkflowState(
        request_id=rid,
        build_id="build-test",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    qp = QueryPlanArtifact(
        artifact_id=uuid.uuid4(),
        request_id=uuid.UUID(rid),
        participant_refs=("p1",),
        rewritten_query="快手晚餐",
        dish_count_requested=4,
        time_constraint_seconds=900,  # 15 分钟
        time_constraint_policy="hard",
        input_fingerprint="0" * 64,
        content_hash="0" * 64,
    )

    state: DietAgentState = {
        "request_id": rid,
        "workflow_state": wf_state,
        "query_plan": qp,
        "safe_recipe_ids": [1, 2, 3, 4, 5],
        "c4": SimpleNamespace(),
        "session_id": "sess_1",
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "tool_context": ToolContext(request_id=rid, build_id="build-test"),
    }

    # 模拟 combine_nutritional_menu 返回 TIME_LIMIT_EXCEEDED
    mock_res = ToolResponse(
        success=False,
        error_code="TIME_LIMIT_EXCEEDED",
        diagnostics={
            "bottleneck": "TIME_LIMIT_EXCEEDED",
            "requested_time_minutes": 15,
            "min_needed_minutes": 26,
            "requested_dish_count": 4,
        },
    )

    with patch(
        "food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu",
        return_value=mock_res,
    ), patch.object(orchestrator, "_guard_active", return_value=None):
        result = orchestrator._node_combine_menu(state)

    assert result["inquiry_needed"] is True
    assert result["diagnosis_code"] == "TIME_LIMIT_EXCEEDED"
    assert "最快制作组合预计需要约 26 分钟" in result["inquiry_reason"]
    assert len(result["inquiry_options"]) == 3
    assert "放宽至 26 分钟" in result["inquiry_options"][0]


def test_graph_e2e_time_limit_exceeded_reaches_clarification() -> None:
    """端到端状态图执行：当烹饪时间超限时，经由条件边自动路由至 inquire_user 并提交 needs_clarification。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=DeterministicScriptedAgentModel())

    rid = _fresh_rid()
    wf_state = WorkflowState(
        request_id=rid,
        build_id="build-test",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_1",
        "message": "我要4个菜，15分钟内搞定",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "build-test",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": SimpleNamespace(
            build_shared_context=lambda *args, **kwargs: (
                SimpleNamespace(session_id="sess_1"),
                None,
            ),
            validate_context_integrity=lambda *args, **kwargs: {"valid": True},
            get_session_state=lambda *args, **kwargs: None,
        ),
        "workflow_state": wf_state,
        "tool_context": ToolContext(request_id=rid, build_id="build-test"),
        "inquiry_needed": False,
        "is_terminal": False,
    }

    mock_search = ToolResponse(
        success=True,
        data={"candidates": [1, 2, 3, 4, 5], "retrieval_result": None, "total": 5},
    )
    mock_audit = ToolResponse(
        success=True,
        data={
            "safe_recipe_ids": [1, 2, 3, 4, 5],
            "excluded_recipe_ids": [],
            "batch": None,
            "excluded_details": {},
        },
    )
    mock_combine = ToolResponse(
        success=False,
        error_code="TIME_LIMIT_EXCEEDED",
        diagnostics={
            "bottleneck": "TIME_LIMIT_EXCEEDED",
            "requested_time_minutes": 15,
            "min_needed_minutes": 28,
            "requested_dish_count": 4,
        },
    )

    finalized_states = []
    with patch(
        "food_agent_v2.c3.graph_orchestrator.search_candidates",
        return_value=mock_search,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu",
        return_value=mock_combine,
    ), patch.object(
        orchestrator, "_guard_active", return_value=None
    ), patch.object(
        orchestrator,
        "_finalize",
        side_effect=lambda st, *args, **kwargs: finalized_states.append(st),
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.ToolHandler"
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize",
        return_value=SimpleNamespace(
            retrieval_query="4个菜 15分钟",
            meal_types=(),
            population_tags=(),
            scenario_tags=(),
            dish_count=4,
            taste_tags=(),
            cuisine_tags=(),
            dish_types=(),
            include_ingredients=(),
            exclude_ingredients=(),
            nutrition_goal_codes=(),
            health_constraints=(),
            time_constraint_seconds=900,
            max_time_minutes=15,
        ),
    ):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    final_wf = finalized_states[0]
    assert final_wf.status == RequestStatus.NEEDS_CLARIFICATION
    assert final_wf.error.error_code == "TIME_LIMIT_EXCEEDED"
    assert "28 分钟" in final_wf.error.message


def test_graph_e2e_safe_candidate_shortage_reaches_clarification() -> None:
    """端到端状态图执行：当健康审查后安全候选不足时，零静默放宽，归因追问并进入 needs_clarification。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=DeterministicScriptedAgentModel())

    rid = _fresh_rid()
    wf_state = WorkflowState(
        request_id=rid,
        build_id="build-test",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_1",
        "message": "我要4个菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "build-test",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": SimpleNamespace(
            build_shared_context=lambda *args, **kwargs: (
                SimpleNamespace(session_id="sess_1"),
                None,
            ),
            validate_context_integrity=lambda *args, **kwargs: {"valid": True},
            get_session_state=lambda *args, **kwargs: None,
        ),
        "workflow_state": wf_state,
        "tool_context": ToolContext(request_id=rid, build_id="build-test"),
        "inquiry_needed": False,
        "is_terminal": False,
    }

    mock_search = ToolResponse(
        success=True,
        data={"candidates": [1, 2, 3, 4], "retrieval_result": None, "total": 4},
    )
    mock_audit = ToolResponse(
        success=True,
        data={
            "safe_recipe_ids": [1],
            "excluded_recipe_ids": [2, 3, 4],
            "batch": None,
            "excluded_details": {
                2: ["p1: 禁忌:花生"],
                3: ["p1: 疾病:高血压"],
                4: ["p1: 禁忌:辣椒"],
            },
        },
    )
    mock_combine = ToolResponse(
        success=False,
        error_code="SAFE_CANDIDATE_SHORTAGE",
        diagnostics={
            "bottleneck": "SAFE_CANDIDATE_SHORTAGE",
            "safe_count": 1,
            "requested_count": 4,
            "deficit": 3,
        },
    )

    finalized_states = []
    with patch(
        "food_agent_v2.c3.graph_orchestrator.search_candidates",
        return_value=mock_search,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu",
        return_value=mock_combine,
    ), patch.object(
        orchestrator, "_guard_active", return_value=None
    ), patch.object(
        orchestrator,
        "_finalize",
        side_effect=lambda st, *args, **kwargs: finalized_states.append(st),
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.ToolHandler"
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize",
        return_value=SimpleNamespace(
            retrieval_query="4个菜",
            meal_types=(),
            population_tags=(),
            scenario_tags=(),
            dish_count=4,
            taste_tags=(),
            cuisine_tags=(),
            dish_types=(),
            include_ingredients=(),
            exclude_ingredients=(),
            nutrition_goal_codes=(),
            health_constraints=(),
            time_constraint_seconds=None,
            max_time_minutes=None,
        ),
    ):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    final_wf = finalized_states[0]
    assert final_wf.status == RequestStatus.NEEDS_CLARIFICATION
    assert final_wf.error.error_code == "SAFE_CANDIDATE_SHORTAGE"
    assert "仅剩 1 道" in final_wf.error.message
    assert "选项 1: 接受推荐当前的 1 道安全菜品" in final_wf.error.message


def test_graph_e2e_happy_path_completes_successfully() -> None:
    """端到端状态图执行：全流程成功路径（检索 → 审查 → 规划 → 校验 → 回答 → 提交）。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=DeterministicScriptedAgentModel())

    rid = _fresh_rid()
    wf_state = WorkflowState(
        request_id=rid,
        build_id="00000000-0000-0000-0000-000000000001",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_1",
        "message": "推荐三道清淡家常菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "00000000-0000-0000-0000-000000000001",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": SimpleNamespace(
            build_shared_context=lambda *args, **kwargs: (
                SimpleNamespace(session_id="sess_1"),
                None,
            ),
            validate_context_integrity=lambda *args, **kwargs: {"valid": True},
            get_session_state=lambda *args, **kwargs: None,
        ),
        "workflow_state": wf_state,
        "tool_context": ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001"),
        "inquiry_needed": False,
        "is_terminal": False,
    }

    mock_search = ToolResponse(
        success=True,
        data={"candidates": [1, 2, 3, 4, 5], "retrieval_result": None, "total": 5},
    )
    mock_audit = ToolResponse(
        success=True,
        data={
            "safe_recipe_ids": [1, 2, 3, 4, 5],
            "excluded_recipe_ids": [],
            "batch": SimpleNamespace(
                safe_recipe_ids=[1, 2, 3, 4, 5],
                excluded_recipe_ids=[],
                participant_recipe_results=[],
            ),
            "excluded_details": {},
        },
    )
    mock_plan = FeasibleMenu(
        plan_id="plan-best",
        recipe_ids=[1, 2, 3],
        menu_hash=menu_hash_for("plan-best", [1, 2, 3]),
        dominant_objective="balanced",
        total_score=92.0,
        time_score=0.9,
        nutrition_score=0.9,
        preference_score=0.9,
        diversity_score=0.9,
        estimated_time_feasible=True,
        estimated_makespan_seconds=1200,
    )
    mock_combine = ToolResponse(
        success=True,
        data={"plans": [mock_plan], "count": 1},
    )

    final_wf_mock = WorkflowState(
        request_id=rid,
        build_id="00000000-0000-0000-0000-000000000001",
        status=RequestStatus.COMPLETED,
        participant_refs=["p1"],
    )

    finalized_states = []
    with patch(
        "food_agent_v2.c3.graph_orchestrator.search_candidates",
        return_value=mock_search,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu",
        return_value=mock_combine,
    ), patch.object(
        orchestrator, "_guard_active", return_value=None
    ), patch.object(
        orchestrator,
        "_select_validate_answer",
        return_value=final_wf_mock,
    ), patch.object(
        orchestrator,
        "_finalize",
        side_effect=lambda st, *args, **kwargs: finalized_states.append(st),
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.ToolHandler.execute",
        autospec=True, side_effect=_execute_with_bound_final_validation
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize",
        return_value=SimpleNamespace(
            retrieval_query="三道 清淡家常菜",
            meal_types=(),
            population_tags=(),
            scenario_tags=(),
            dish_count=3,
            taste_tags=("清淡",),
            cuisine_tags=(),
            dish_types=(),
            include_ingredients=(),
            exclude_ingredients=(),
            nutrition_goal_codes=(),
            health_constraints=(),
            time_constraint_seconds=None,
            max_time_minutes=None,
        ),
    ):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    final_wf = finalized_states[0]
    assert final_wf.status == RequestStatus.COMPLETED


# ============================================================================
# 3. 整改清单 R1 / R2 回归测试
# ============================================================================


def test_r1_unspecified_dish_count_plans_once_and_matches_plan_id_and_menu_hash() -> None:
    """R1 回归：未指定菜数时默认使用 MenuHardConstraints 默认值 (5 菜)，

    且规划只执行一次，图状态与 tool_context 中的 plan_id/menu_hash 严格一致，
    不出现 UNKNOWN_PLAN_ID 或 MENU_HASH_MISMATCH。
    """
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    build_id = "00000000-0000-0000-0000-000000000001"

    wf_state = WorkflowState(
        request_id=rid,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    # 未指定菜数 (dish_count_requested=None)
    qp = QueryPlanArtifact(
        artifact_id=uuid.uuid4(),
        request_id=uuid.UUID(rid),
        participant_refs=("p1",),
        rewritten_query="家常菜",
        dish_count_requested=None,
        time_constraint_seconds=None,
        time_constraint_policy="flexible",
        input_fingerprint="0" * 64,
        content_hash="0" * 64,
    )
    tool_ctx = ToolContext(
        request_id=rid,
        node_id="health_menu_planning",
        build_id=build_id,
        participant_user_mapping={"p1": 1},
    )
    tool_ctx.previous_results["query_plan"] = qp

    safe_ids = [101, 102, 103, 104, 105, 106]
    tool_ctx.safe_recipe_ids = safe_ids
    from food_agent_v2.b4.schemas import HealthEvaluationReceipt

    batch = HealthEvaluationReceipt(
        evaluation_id=str(uuid.uuid4()),
        request_id=rid,
        retrieval_result_ref=str(uuid.uuid4()),
        constraint_set_refs=["cs_1"],
        participant_recipe_results=[],
        safe_recipe_ids=safe_ids,
        excluded_recipe_ids=[],
        input_fingerprint="0" * 64,
    )
    tool_ctx.previous_results["health_evaluation"] = batch

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_r1",
        "workflow_state": wf_state,
        "query_plan": qp,
        "safe_recipe_ids": safe_ids,
        "c4": SimpleNamespace(),
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "tool_context": tool_ctx,
    }

    # 捕获规划调用次数与约束参数
    plan_calls = []
    from food_agent_v2.c2.schemas import menu_hash_for

    mock_plan_id = "plan-r1-5dishes"
    mock_recipe_ids = [101, 102, 103, 104, 105]
    mock_hash = menu_hash_for(mock_plan_id, mock_recipe_ids)
    mock_feasible = FeasibleMenu(
        plan_id=mock_plan_id,
        recipe_ids=mock_recipe_ids,
        menu_hash=menu_hash_for(mock_plan_id, mock_recipe_ids),
        dominant_objective="balanced",
        total_score=90.0,
        time_score=0.9,
        nutrition_score=0.9,
        preference_score=0.9,
        diversity_score=0.9,
        estimated_time_feasible=True,
        estimated_makespan_seconds=1200,
    )

    def mock_plan(constraints, *args, **kwargs):
        plan_calls.append(constraints)
        return [mock_feasible]

    with patch("food_agent_v2.c2.MenuPlanner.plan", side_effect=mock_plan), \
         patch.object(orchestrator, "_guard_active", return_value=None):
        result = orchestrator._node_combine_menu(state)

    # 断言 1：规划只执行了恰好 1 次（消除重复规划）
    assert len(plan_calls) == 1, f"规划应只执行 1 次，实际执行了 {len(plan_calls)} 次"
    # 断言 2：默认菜数是 5（MenuHardConstraints 默认值），绝非旧代码的 4
    assert plan_calls[0].dish_count == 5

    # 断言 3：图状态与 tool_context 中的 plan_id 与 menu_hash 完全一致
    assert "feasible_menus" in result
    plans_in_state = result["feasible_menus"]
    plans_in_ctx = tool_ctx.previous_results.get("feasible_menus", [])
    assert len(plans_in_state) == 1
    assert plans_in_state[0].plan_id == mock_plan_id
    assert plans_in_state[0].menu_hash == mock_hash
    assert plans_in_ctx[0].plan_id == mock_plan_id
    assert plans_in_ctx[0].menu_hash == mock_hash

    # 断言 4：通过 validate_selected_menu_health 检验不会出现 UNKNOWN_PLAN_ID 或 MENU_HASH_MISMATCH
    from food_agent_v2.c3.tool_handler import ToolHandler
    handler = ToolHandler(tool_ctx)
    with patch("food_agent_v2.b3.recipe_views.get_view_builder") as mock_bld, \
         patch("food_agent_v2.b4.HealthRuleEngine") as mock_hre, \
         patch("food_agent_v2.b2.UserHealthProfileService") as mock_uhp:
        mock_bld.return_value.build_health_ingredient_view.return_value = None
        mock_engine = MagicMock()
        mock_engine.validate_selected_menu_occurrences.return_value = SimpleNamespace(
            verdict="PASS",
            participant_recipe_results=[],
        )
        mock_hre.return_value = mock_engine
        mock_b2 = MagicMock()
        mock_b2.derive_constraints.return_value = SimpleNamespace(hard_constraints=[])
        mock_uhp.return_value = mock_b2

        val_res = handler.execute(
            "validate_selected_menu_health",
            {"plan_id": mock_plan_id, "recipe_ids": mock_recipe_ids},
        )
        assert "error" not in val_res, f"校验不应报错: {val_res.get('error')}"
        assert val_res.get("verdict") == "PASS"
        assert val_res.get("plan_id") == mock_plan_id
        assert val_res.get("menu_hash") == mock_hash


def test_r1_specified_dish_count_uses_query_plan_and_plans_once() -> None:
    """R1 回归：明确指定菜数时（如 3 菜），使用已验证 QueryPlan 中的 dish_count_requested，

    规划执行且仅执行 1 次，不使用默认 5 菜，也不硬编码 4 菜。
    """
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    build_id = "00000000-0000-0000-0000-000000000001"

    wf_state = WorkflowState(
        request_id=rid,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    qp = QueryPlanArtifact(
        artifact_id=uuid.uuid4(),
        request_id=uuid.UUID(rid),
        participant_refs=("p1",),
        rewritten_query="家常菜 3个菜",
        dish_count_requested=3,
        time_constraint_seconds=None,
        time_constraint_policy="flexible",
        input_fingerprint="0" * 64,
        content_hash="0" * 64,
    )
    tool_ctx = ToolContext(
        request_id=rid,
        node_id="health_menu_planning",
        build_id=build_id,
        participant_user_mapping={"p1": 1},
    )
    tool_ctx.previous_results["query_plan"] = qp

    safe_ids = [101, 102, 103, 104]
    tool_ctx.safe_recipe_ids = safe_ids
    from food_agent_v2.b4.schemas import HealthEvaluationReceipt

    batch = HealthEvaluationReceipt(
        evaluation_id=str(uuid.uuid4()),
        request_id=rid,
        retrieval_result_ref=str(uuid.uuid4()),
        constraint_set_refs=["cs_1"],
        participant_recipe_results=[],
        safe_recipe_ids=safe_ids,
        excluded_recipe_ids=[],
        input_fingerprint="0" * 64,
    )
    tool_ctx.previous_results["health_evaluation"] = batch

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_r1_spec",
        "workflow_state": wf_state,
        "query_plan": qp,
        "safe_recipe_ids": safe_ids,
        "c4": SimpleNamespace(),
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "tool_context": tool_ctx,
    }

    plan_calls = []
    from food_agent_v2.c2.schemas import menu_hash_for

    mock_plan_id = "plan-r1-3dishes"
    mock_recipe_ids = [101, 102, 103]
    mock_hash = menu_hash_for(mock_plan_id, mock_recipe_ids)
    mock_feasible = FeasibleMenu(
        plan_id=mock_plan_id,
        recipe_ids=mock_recipe_ids,
        menu_hash=mock_hash,
        dominant_objective="balanced",
        total_score=90.0,
        time_score=0.9,
        nutrition_score=0.9,
        preference_score=0.9,
        diversity_score=0.9,
        estimated_time_feasible=True,
        estimated_makespan_seconds=900,
    )

    def mock_plan(constraints, *args, **kwargs):
        plan_calls.append(constraints)
        return [mock_feasible]

    with patch("food_agent_v2.c2.MenuPlanner.plan", side_effect=mock_plan), \
         patch.object(orchestrator, "_guard_active", return_value=None):
        result = orchestrator._node_combine_menu(state)

    assert len(plan_calls) == 1
    assert plan_calls[0].dish_count == 3
    assert result["feasible_menus"][0].plan_id == mock_plan_id


def test_r2_retrieval_failure_fails_closed_no_inquiry_no_commit() -> None:
    """R2 回归：检索服务异常时，必须 fail-closed，记录失败回执，直接进入 finish_error，不追问、不提交。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    build_id = "00000000-0000-0000-0000-000000000001"

    wf_state = WorkflowState(
        request_id=rid,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        node_id="query_understanding",
        build_id=build_id,
        participant_user_mapping={"p1": 1},
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_r2_retrieval",
        "workflow_state": wf_state,
        "intent": SimpleNamespace(
            intent="new_recommendation",
            rewritten_query="爆炒牛肉",
            query="爆炒牛肉",
            target_recipe_id=None,
        ),
        "query_plan": None,
        "user_id_mapping": {"p1": 1},
        "build_id": build_id,
        "c4": SimpleNamespace(get_session_state=lambda *args, **kwargs: None),
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "tool_context": tool_ctx,
    }

    mock_error_res = ToolResponse(
        success=False,
        status="error",
        error_code="RETRIEVAL_FAILED",
        message="检索服务连接超时",
    )

    with patch(
        "food_agent_v2.c3.graph_orchestrator.search_candidates",
        return_value=mock_error_res,
    ), patch.object(orchestrator, "_guard_active", return_value=None):
        node_res = orchestrator._node_search_candidates(state)

    # 断言 1：返回 terminal 状态，携带真实错误码
    assert node_res.get("is_terminal") is True
    assert node_res.get("error_code") == "RETRIEVAL_FAILED"
    assert "inquiry_needed" not in node_res or not node_res["inquiry_needed"]

    # 断言 2：工作流状态置为 FAILED，且错误码为 RETRIEVAL_FAILED
    failed_wf = node_res["workflow_state"]
    assert failed_wf.status == RequestStatus.FAILED
    assert failed_wf.error is not None
    assert failed_wf.error.error_code == "RETRIEVAL_FAILED"

    # 断言 3：记录的工具回执 success=False，记录了真实错误码
    receipts = tool_ctx.tool_receipts
    assert len(receipts) == 1
    assert receipts[0]["tool_name"] == "retrieve_recipes"
    assert receipts[0]["success"] is False
    assert receipts[0]["error_code"] == "RETRIEVAL_FAILED"


def test_r2_temporary_constraint_load_failed_fails_closed_no_success_receipt() -> None:
    """R2 回归：临时约束加载失败时，必须 fail-closed，记录失败回执，绝不记录 success=True 或冒充无安全菜。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    build_id = "00000000-0000-0000-0000-000000000001"

    wf_state = WorkflowState(
        request_id=rid,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        node_id="health_menu_planning",
        build_id=build_id,
        participant_user_mapping={"p1": 1},
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_r2_temp_fail",
        "workflow_state": wf_state,
        "candidate_recipes": [1, 2, 3],
        "intent": None,
        "build_id": build_id,
        "user_id_mapping": {"p1": 1},
        "c4": SimpleNamespace(get_session_state=lambda *args, **kwargs: None),
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "tool_context": tool_ctx,
    }

    mock_audit_res = ToolResponse(
        success=False,
        status="error",
        error_code="TEMPORARY_CONSTRAINT_LOAD_FAILED",
        message="Redis 连接拒绝",
    )

    with patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit_res,
    ), patch.object(orchestrator, "_guard_active", return_value=None):
        node_res = orchestrator._node_audit_health(state)

    # 断言 1：返回 terminal 状态，绝不转为 inquiry_needed
    assert node_res.get("is_terminal") is True
    assert node_res.get("error_code") == "TEMPORARY_CONSTRAINT_LOAD_FAILED"
    assert "inquiry_needed" not in node_res or not node_res["inquiry_needed"]

    # 断言 2：工作流状态置为 FAILED
    failed_wf = node_res["workflow_state"]
    assert failed_wf.status == RequestStatus.FAILED
    assert failed_wf.error is not None
    assert failed_wf.error.error_code == "TEMPORARY_CONSTRAINT_LOAD_FAILED"

    # 断言 3：记录的工具回执 success=False，绝不伪造 success=True
    receipts = tool_ctx.tool_receipts
    assert len(receipts) == 1
    assert receipts[0]["tool_name"] == "evaluate_recipe_health"
    assert receipts[0]["success"] is False
    assert receipts[0]["error_code"] == "TEMPORARY_CONSTRAINT_LOAD_FAILED"


def test_r2_health_coverage_incomplete_fails_closed() -> None:
    """R2 回归：健康规则覆盖不完整时，必须 fail-closed，记录失败回执，进入 FAILED 终态。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    build_id = "00000000-0000-0000-0000-000000000001"

    wf_state = WorkflowState(
        request_id=rid,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        node_id="health_menu_planning",
        build_id=build_id,
        participant_user_mapping={"p1": 1},
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_r2_cov_fail",
        "workflow_state": wf_state,
        "candidate_recipes": [1, 2, 3],
        "intent": None,
        "build_id": build_id,
        "user_id_mapping": {"p1": 1},
        "c4": SimpleNamespace(get_session_state=lambda *args, **kwargs: None),
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "tool_context": tool_ctx,
    }

    mock_audit_res = ToolResponse(
        success=False,
        status="error",
        error_code="HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE",
        message="规则覆盖不完整",
    )

    with patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit_res,
    ), patch.object(orchestrator, "_guard_active", return_value=None):
        node_res = orchestrator._node_audit_health(state)

    assert node_res.get("is_terminal") is True
    assert node_res.get("error_code") == "HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE"
    failed_wf = node_res["workflow_state"]
    assert failed_wf.status == RequestStatus.FAILED
    assert failed_wf.error.error_code == "HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE"

    receipts = tool_ctx.tool_receipts
    assert len(receipts) == 1
    assert receipts[0]["success"] is False
    assert receipts[0]["error_code"] == "HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE"


def test_r2_legitimate_zero_safe_candidates_enters_clarification() -> None:
    """R2 回归：在健康审查完整执行后（B4 审查客观成功），若所有候选均被排除，合法转入 NO_SAFE_CANDIDATE 追问。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    build_id = "00000000-0000-0000-0000-000000000001"

    wf_state = WorkflowState(
        request_id=rid,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        node_id="health_menu_planning",
        build_id=build_id,
        participant_user_mapping={"p1": 1},
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_r2_no_safe",
        "workflow_state": wf_state,
        "candidate_recipes": [1, 2],
        "intent": None,
        "build_id": build_id,
        "user_id_mapping": {"p1": 1},
        "c4": SimpleNamespace(get_session_state=lambda *args, **kwargs: None),
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "tool_context": tool_ctx,
    }

    mock_audit_res = ToolResponse(
        success=False,
        status="no_solution",
        error_code="NO_SAFE_CANDIDATE",
        data={
            "safe_recipe_ids": [],
            "excluded_recipe_ids": [1, 2],
            "batch": SimpleNamespace(
                safe_recipe_ids=[],
                excluded_recipe_ids=[1, 2],
                participant_recipe_results=[],
            ),
            "excluded_details": {1: ["p1: 禁忌:花生"], 2: ["p1: 疾病:高血压"]},
        },
    )

    with patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit_res,
    ), patch.object(orchestrator, "_guard_active", return_value=None):
        node_res = orchestrator._node_audit_health(state)

    # 合法业务分支：转入追问
    assert node_res.get("inquiry_needed") is True
    assert node_res.get("diagnosis_code") == "NO_SAFE_CANDIDATE"

    # 工具回执记录客观审查事实 (success=True)
    receipts = tool_ctx.tool_receipts
    assert len(receipts) == 1
    assert receipts[0]["tool_name"] == "evaluate_recipe_health"
    assert receipts[0]["success"] is True
    assert receipts[0]["error_code"] is None


# ============================================================================
# 5. R3: 替换目标解析与约束继承回归测试
# ============================================================================


class _FakeMultiTurnC4:
    def __init__(
        self,
        current_menu: dict | None = None,
        query_plan: dict | None = None,
        menu_history: list[dict] | None = None,
    ) -> None:
        self.current_menu = current_menu
        self.query_plan = query_plan
        self.menu_history = menu_history or []

    def build_shared_context(
        self, session_id, participant_refs, raw, mapping, request_id=None, build_id=None
    ):
        return SimpleNamespace(session_id=session_id), {}

    def validate_context_integrity(self, ref):
        return {"valid": True}

    def project_model_context(self, role, handoff, ref):
        return SimpleNamespace(
            role=role, conversation_visible=[], constraint_visible=[], menu_visible={}
        )

    def get_session_state(self, session_id):
        if self.current_menu is None:
            return None
        return {
            "session_id": session_id,
            "current_menu": self.current_menu,
            "query_plan": self.query_plan,
            "menu_history": self.menu_history,
        }

    def to_b4_constraints(self, session_id):
        return []

    def commit_menu(self, *args, **kwargs):
        pass

    def release_session_lock(self, session_id, lock_token):
        pass


def test_r3_replace_exact_named_dish_rejects_target_and_locks_remainder() -> None:
    """R3: 当前四菜，换掉其中一个具名菜：目标进入 rejected，其余三菜锁定，菜数维持 4，仅换 1 个槽位。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=DeterministicScriptedAgentModel())
    rid = _fresh_rid()
    build_id = "00000000-0000-0000-0000-000000000001"

    current_menu = {
        "plan_id": "plan-curr",
        "recipe_ids": [1, 2, 3, 4],
        "items": [
            {"recipe_id": 1, "name": "番茄鸡蛋汤"},
            {"recipe_id": 2, "name": "青椒肉丝"},
            {"recipe_id": 3, "name": "清炒时蔬"},
            {"recipe_id": 4, "name": "宫保鸡丁"},
        ],
    }
    c4 = _FakeMultiTurnC4(
        current_menu=current_menu,
        query_plan={"dish_count_requested": 4, "meal_types": ["晚餐"]},
    )

    wf_state = WorkflowState(
        request_id=rid,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        build_id=build_id,
        participant_user_mapping={"p1": 1},
        session_id="sess_r3_replace",
        context_service=c4,
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_r3_replace",
        "message": "把青椒肉丝换成红烧茄子",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": build_id,
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
        "inquiry_needed": False,
        "is_terminal": False,
    }

    # 1. 验证理解节点解析 target_recipe_id 并锁定 4 菜
    with patch(
        "food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize",
        return_value=SimpleNamespace(
            retrieval_query="红烧茄子",
            dish_count=4,
            meal_types=("晚餐",),
            population_tags=(),
            scenario_tags=(),
            taste_tags=(),
            cuisine_tags=(),
            dish_types=(),
            include_ingredients=(),
            exclude_ingredients=(),
            nutrition_goal_codes=(),
            health_constraints=(),
            time_constraint_seconds=None,
            max_time_minutes=None,
        ),
    ):
        res_intent = orchestrator._node_understand_intent(state)

    assert res_intent["intent"].intent == "replace"
    assert res_intent["intent"].target_recipe_id == 2  # 青椒肉丝 ID 为 2
    assert res_intent["query_plan"].dish_count_requested == 4  # 保持 4 菜

    # 2. 验证审核节点将目标 2 放入 rejected，其余 1, 3, 4 放入 locked
    state_after_intent = {**state, **res_intent, "candidate_recipes": [1, 2, 3, 4, 5]}
    mock_audit = ToolResponse(
        success=True,
        status="ok",
        data={
            "safe_recipe_ids": [1, 2, 3, 4, 5],
            "excluded_recipe_ids": [],
            "batch": SimpleNamespace(safe_recipe_ids=[1, 2, 3, 4, 5]),
            "excluded_details": {},
        },
    )

    with patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch.object(orchestrator, "_guard_active", return_value=None):
        res_audit = orchestrator._node_audit_health(state_after_intent)

    assert res_audit["rejected_recipe_ids"] == [2]
    assert set(res_audit["locked_recipe_ids"]) == {1, 3, 4}

    # 3. 验证端到端通过 LangGraph：最终只替换 1 个槽位，目标菜不回流，原三菜保留
    mock_search = ToolResponse(
        success=True,
        status="ok",
        data={"candidates": [5], "retrieval_result": None, "total": 1},
    )
    new_plan = FeasibleMenu(
        plan_id="plan-replaced-1",
        menu_hash=menu_hash_for("plan-replaced-1", [1, 3, 4, 5]),
        recipe_ids=[1, 3, 4, 5],
        dominant_objective="nutrition",
        total_score=95.0,
        estimated_makespan_seconds=1200,
    )
    mock_combine = ToolResponse(
        success=True,
        status="ok",
        data={"plans": [new_plan], "count": 1},
    )

    finalized_states = []
    with patch(
        "food_agent_v2.c3.graph_orchestrator.search_candidates",
        return_value=mock_search,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu",
        return_value=mock_combine,
    ), patch.object(
        orchestrator, "_guard_active", return_value=None
    ), patch.object(
        ToolHandler, "execute", autospec=True,
        side_effect=_execute_with_bound_final_validation,
    ), patch.object(
        orchestrator,
        "_select_validate_answer",
        side_effect=lambda st, ctx, plans, *args, **kwargs: reduce_workflow_state(
            st, action="unified_review", status="PASS"
        ),
    ), patch.object(
        orchestrator,
        "_finalize",
        side_effect=lambda st, *args, **kwargs: finalized_states.append(st),
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize",
        return_value=SimpleNamespace(
            retrieval_query="红烧茄子",
            dish_count=4,
            meal_types=("晚餐",),
            population_tags=(),
            scenario_tags=(),
            taste_tags=(),
            cuisine_tags=(),
            dish_types=(),
            include_ingredients=(),
            exclude_ingredients=(),
            nutrition_goal_codes=(),
            health_constraints=(),
            time_constraint_seconds=None,
            max_time_minutes=None,
        ),
    ):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.COMPLETED
    assert 2 not in new_plan.recipe_ids
    assert {1, 3, 4}.issubset(set(new_plan.recipe_ids))
    assert len(new_plan.recipe_ids) == 4


def test_r3_replace_without_current_menu_needs_clarification() -> None:
    """R3: 会话无前文菜单时发起替换，明确进入澄清终态，不猜测 ID 或盲目推荐。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    c4 = _FakeMultiTurnC4(current_menu=None)

    wf_state = WorkflowState(
        request_id=rid,
        build_id="build-test",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        build_id="build-test",
        participant_user_mapping={"p1": 1},
        session_id="sess_no_menu",
        context_service=c4,
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_no_menu",
        "message": "把青椒肉丝换掉",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "build-test",
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
        "inquiry_needed": False,
        "is_terminal": False,
    }

    res = orchestrator._node_understand_intent(state)
    assert res.get("inquiry_needed") is True
    assert res.get("diagnosis_code") == "NO_CURRENT_MENU"
    assert "当前没有可操作的菜单" in res.get("inquiry_reason")


def test_r3_replace_ambiguous_target_needs_clarification() -> None:
    """R3: 替换菜名未命中或命中多道菜时，明确澄清，绝不猜测 ID。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    current_menu = {
        "plan_id": "plan-curr",
        "recipe_ids": [1, 2, 3],
        "items": [
            {"recipe_id": 1, "name": "番茄鸡蛋汤"},
            {"recipe_id": 2, "name": "青椒肉丝"},
            {"recipe_id": 3, "name": "清炒时蔬"},
        ],
    }
    c4 = _FakeMultiTurnC4(current_menu=current_menu)

    wf_state = WorkflowState(
        request_id=rid,
        build_id="build-test",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        build_id="build-test",
        participant_user_mapping={"p1": 1},
        session_id="sess_ambig",
        context_service=c4,
    )

    # 1. 菜名不存在："水煮肉片" 不在菜单中
    state1: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_ambig",
        "message": "把水煮肉片换掉",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "build-test",
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }
    res1 = orchestrator._node_understand_intent(state1)
    assert res1.get("inquiry_needed") is True
    assert res1.get("diagnosis_code") == "AMBIGUOUS_REPLACE_TARGET"
    assert "请明确要替换的当前菜名" in res1.get("inquiry_reason")

    # 2. 同时命中多道菜："把番茄鸡蛋汤和青椒肉丝换掉"
    state2: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_ambig",
        "message": "把番茄鸡蛋汤和青椒肉丝换掉",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "build-test",
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }
    res2 = orchestrator._node_understand_intent(state2)
    assert res2.get("inquiry_needed") is True
    assert res2.get("diagnosis_code") == "AMBIGUOUS_REPLACE_TARGET"
    assert "请明确要替换的当前菜名" in res2.get("inquiry_reason")


def test_r3_replace_inherits_previous_query_plan_constraints() -> None:
    """R3: 替换请求正确继承原 QueryPlan 的餐次、忌口、硬时限及菜数约束。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    current_menu = {
        "plan_id": "plan-curr",
        "recipe_ids": [1, 2, 3, 4],
        "items": [
            {"recipe_id": 1, "name": "番茄鸡蛋汤"},
            {"recipe_id": 2, "name": "青椒肉丝"},
            {"recipe_id": 3, "name": "清炒时蔬"},
            {"recipe_id": 4, "name": "宫保鸡丁"},
        ],
    }
    prior_plan = {
        "meal_types": ["晚餐"],
        "exclude_ingredients": ["花生"],
        "nutrition_goal_codes": ["LOW_SALT"],
        "dish_count_requested": 4,
        "time_constraint_seconds": 1800,
        "time_constraint_policy": "hard",
    }
    c4 = _FakeMultiTurnC4(current_menu=current_menu, query_plan=prior_plan)

    wf_state = WorkflowState(
        request_id=rid,
        build_id="build-test",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        build_id="build-test",
        participant_user_mapping={"p1": 1},
        session_id="sess_inherit",
        context_service=c4,
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_inherit",
        "message": "把青椒肉丝换成红烧茄子",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "build-test",
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    captured_prior = {}

    def _mock_normalize(message, participants, previous_query_plan=None):
        captured_prior["plan"] = previous_query_plan
        return SimpleNamespace(
            retrieval_query="红烧茄子",
            dish_count=4,
            meal_types=tuple(previous_query_plan.get("meal_types", ())),
            population_tags=(),
            scenario_tags=(),
            taste_tags=(),
            cuisine_tags=(),
            dish_types=(),
            include_ingredients=(),
            exclude_ingredients=tuple(previous_query_plan.get("exclude_ingredients", ())),
            nutrition_goal_codes=tuple(previous_query_plan.get("nutrition_goal_codes", ())),
            health_constraints=(),
            time_constraint_seconds=previous_query_plan.get("time_constraint_seconds"),
            max_time_minutes=30,
        )

    with patch(
        "food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize",
        side_effect=_mock_normalize,
    ):
        res = orchestrator._node_understand_intent(state)

    # 验证传递给 QueryNormalizer 的上一轮硬约束
    assert captured_prior["plan"] == prior_plan
    # 验证新生成的 QueryPlanArtifact 继承了原时限与菜数
    qp: QueryPlanArtifact = res["query_plan"]
    assert qp.dish_count_requested == 4
    assert qp.time_constraint_policy == "hard"
    assert qp.time_constraint_seconds == 1800
    assert qp.exclude_ingredients == ("花生",)


def test_r3_replace_locked_dish_unsafe_aborts_without_silent_expansion() -> None:
    """R3: 共同审查时，若原菜单中锁定的保留菜品不再安全，明确终止/追问，不偷偷扩大替换范围。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()
    current_menu = {
        "plan_id": "plan-curr",
        "recipe_ids": [1, 2, 3, 4],
        "items": [
            {"recipe_id": 1, "name": "番茄鸡蛋汤"},
            {"recipe_id": 2, "name": "青椒肉丝"},
            {"recipe_id": 3, "name": "清炒时蔬"},
            {"recipe_id": 4, "name": "宫保鸡丁"},
        ],
    }
    c4 = _FakeMultiTurnC4(current_menu=current_menu)

    wf_state = WorkflowState(
        request_id=rid,
        build_id="00000000-0000-0000-0000-000000000001",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        build_id="00000000-0000-0000-0000-000000000001",
        participant_user_mapping={"p1": 1},
        session_id="sess_unsafe_locked",
        context_service=c4,
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_unsafe_locked",
        "message": "把青椒肉丝换成红烧茄子",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "00000000-0000-0000-0000-000000000001",
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
        "intent": IntentDelta(intent="replace", target_recipe_id=2),
        "candidate_recipes": [1, 2, 3, 4, 5],
    }

    # 模拟健康审查：保留菜 3（清炒时蔬）被判定为不安全（safe_ids 中仅有 1, 4, 5）
    mock_audit = ToolResponse(
        success=True,
        status="ok",
        data={
            "safe_recipe_ids": [1, 4, 5],
            "excluded_recipe_ids": [2, 3],
            "batch": SimpleNamespace(safe_recipe_ids=[1, 4, 5]),
            "excluded_details": {3: ["p1: 农药超标/特定忌口"]},
        },
    )

    with patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch.object(orchestrator, "_guard_active", return_value=None):
        res = orchestrator._node_audit_health(state)

    # 必须终止并触发追问，绝不能将 3 默默交给 planner 替换（不偷偷扩大替换范围）
    assert res.get("inquiry_needed") is True
    assert res.get("diagnosis_code") == "LOCKED_RECIPE_UNSAFE"
    assert "清炒时蔬" in res.get("inquiry_reason")


# ============================================================================
# 6. R4: 恢复请求读取业务历史回归测试
# ============================================================================


def test_r4_restore_commits_target_historical_version_without_retrieval() -> None:
    """R4: 两个不同历史菜单的恢复：准确读取目标版本，菜品及 QueryPlan 匹配，检索调用次数为零。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=DeterministicScriptedAgentModel())
    rid = _fresh_rid()
    build_id = "00000000-0000-0000-0000-000000000001"

    # 历史有两版：第一版 [5, 6, 3]（晚餐），第二版 [1, 2, 3]（午餐）
    version1 = {
        "plan_id": "plan-v1",
        "recipe_ids": [5, 6, 3],
        "query_plan": {"meal_types": ["晚餐"], "rewritten_query": "清淡晚餐"},
    }
    version2 = {
        "plan_id": "plan-v2",
        "recipe_ids": [1, 2, 3],
        "query_plan": {"meal_types": ["午餐"], "rewritten_query": "家常午餐"},
    }
    current_menu = {
        "plan_id": "plan-v2",
        "recipe_ids": [1, 2, 3],
        "items": [
            {"recipe_id": 1, "name": "番茄鸡蛋汤"},
            {"recipe_id": 2, "name": "青椒肉丝"},
            {"recipe_id": 3, "name": "清炒时蔬"},
        ],
    }

    c4 = _FakeMultiTurnC4(
        current_menu=current_menu,
        menu_history=[version1, version2],
    )

    wf_state = WorkflowState(
        request_id=rid,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        build_id=build_id,
        participant_user_mapping={"p1": 1},
        session_id="sess_r4_restore",
        context_service=c4,
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_r4_restore",
        "message": "恢复上一版",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": build_id,
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
        "inquiry_needed": False,
        "is_terminal": False,
    }

    mock_builder = SimpleNamespace(
        build_retrieval_view=lambda rid: SimpleNamespace(recipe_id=rid, name=f"菜品-{rid}")
    )

    # 1. 验证理解意图节点读取上一版 [5, 6, 3]，无需检索
    with patch(
        "food_agent_v2.b3.recipe_views.get_view_builder",
        return_value=mock_builder,
    ):
        res_intent = orchestrator._node_understand_intent(state)

    assert res_intent["intent"].intent == "restore"
    assert res_intent["candidate_recipes"] == [5, 6, 3]
    assert res_intent["locked_recipe_ids"] == [5, 6, 3]
    qp: QueryPlanArtifact = res_intent["query_plan"]
    assert qp.dish_count_requested == 3
    assert qp.meal_types == ("晚餐",)

    # 2. 验证路由分支：restore 意图直接路由至 decide，Agent 决策直接选择 audit_recipe_health，跳过 search_candidates
    route = orchestrator._route_after_intent({**state, **res_intent})
    assert route == "decide"
    next_action = orchestrator._decide_next_action({**state, **res_intent}, AgentPolicy())
    assert next_action.action == ActionType.AUDIT_RECIPE_HEALTH

    # 3. 端到端执行：断言检索工具调用次数绝对为 0，最终提交历史菜单 [5, 6, 3]
    search_mock = MagicMock(side_effect=RuntimeError("restore must not call search_candidates"))
    mock_audit = ToolResponse(
        success=True,
        status="ok",
        data={
            "safe_recipe_ids": [5, 6, 3],
            "excluded_recipe_ids": [],
            "batch": SimpleNamespace(safe_recipe_ids=[5, 6, 3]),
            "excluded_details": {},
        },
    )
    restored_plan = FeasibleMenu(
        plan_id="plan-restored-v1",
        menu_hash=menu_hash_for("plan-restored-v1", [5, 6, 3]),
        recipe_ids=[5, 6, 3],
        dominant_objective="nutrition",
        total_score=90.0,
        estimated_makespan_seconds=1500,
    )
    mock_combine = ToolResponse(
        success=True,
        status="ok",
        data={"plans": [restored_plan], "count": 1},
    )

    finalized_states = []
    with patch(
        "food_agent_v2.b3.recipe_views.get_view_builder",
        return_value=mock_builder,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.search_candidates",
        search_mock,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu",
        return_value=mock_combine,
    ), patch.object(
        orchestrator, "_guard_active", return_value=None
    ), patch.object(
        ToolHandler, "execute", autospec=True,
        side_effect=_execute_with_bound_final_validation,
    ), patch.object(
        orchestrator,
        "_select_validate_answer",
        side_effect=lambda st, ctx, plans, *args, **kwargs: reduce_workflow_state(
            st, action="unified_review", status="PASS"
        ),
    ), patch.object(
        orchestrator,
        "_finalize",
        side_effect=lambda st, *args, **kwargs: finalized_states.append(st),
    ):
        orchestrator._graph.invoke(state)

    assert search_mock.call_count == 0  # 检索调用次数为零
    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.COMPLETED
    assert restored_plan.recipe_ids == [5, 6, 3]


def test_r4_restore_without_history_needs_clarification() -> None:
    """R4: 会话无历史菜单时恢复，触发澄清终态，不猜测或重新推荐。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()

    current_menu = {
        "plan_id": "plan-v1",
        "recipe_ids": [1, 2, 3],
    }
    c4 = _FakeMultiTurnC4(
        current_menu=current_menu,
        menu_history=[current_menu],  # 仅有当前版本，无上一版
    )

    wf_state = WorkflowState(
        request_id=rid,
        build_id="build-test",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        build_id="build-test",
        participant_user_mapping={"p1": 1},
        session_id="sess_no_hist",
        context_service=c4,
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_no_hist",
        "message": "恢复上一版",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "build-test",
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    res = orchestrator._node_understand_intent(state)
    assert res.get("inquiry_needed") is True
    assert res.get("diagnosis_code") == "NO_PREVIOUS_MENU"
    assert "没有可恢复的上一版菜单" in res.get("inquiry_reason")


def test_r4_restore_recipe_unavailable_fails_closed() -> None:
    """R4: 上一版菜谱在当前构建中不可用时，严格 fail-closed，不重新推荐冒充恢复。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()

    version1 = {"plan_id": "plan-v1", "recipe_ids": [999]}
    version2 = {"plan_id": "plan-v2", "recipe_ids": [1, 2, 3]}
    c4 = _FakeMultiTurnC4(
        current_menu=version2,
        menu_history=[version1, version2],
    )

    wf_state = WorkflowState(
        request_id=rid,
        build_id="build-test",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        build_id="build-test",
        participant_user_mapping={"p1": 1},
        session_id="sess_unavail",
        context_service=c4,
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_unavail",
        "message": "恢复上一版",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "build-test",
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    mock_builder = SimpleNamespace(
        build_retrieval_view=lambda rid: None  # 菜品 999 不可用
    )

    with patch(
        "food_agent_v2.b3.recipe_views.get_view_builder",
        return_value=mock_builder,
    ):
        res = orchestrator._node_understand_intent(state)

    assert res.get("is_terminal") is True
    assert res.get("error_code") == "RESTORE_VERSION_UNAVAILABLE"
    wf: WorkflowState = res["workflow_state"]
    assert wf.status == RequestStatus.FAILED
    assert wf.error.error_code == "RESTORE_VERSION_UNAVAILABLE"


def test_r4_restore_historical_recipes_violating_health_fails_closed() -> None:
    """R4: 历史菜品在当前健康规则下不安全时，进入 no_safe_menu 终态，绝不生成替代菜单冒充恢复。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    rid = _fresh_rid()

    wf_state = WorkflowState(
        request_id=rid,
        build_id="00000000-0000-0000-0000-000000000001",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )
    tool_ctx = ToolContext(
        request_id=rid,
        build_id="00000000-0000-0000-0000-000000000001",
        participant_user_mapping={"p1": 1},
        session_id="sess_violating",
    )

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_violating",
        "message": "恢复上一版",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "00000000-0000-0000-0000-000000000001",
        "lock_token": "token_1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": SimpleNamespace(get_session_state=lambda *args, **kwargs: None),
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
        "intent": IntentDelta(intent="restore"),
        "candidate_recipes": [5, 6, 3],  # 历史菜品包含 5, 6, 3
    }

    # 模拟健康审查：菜品 5 被排除（仅 6, 3 安全）
    mock_audit = ToolResponse(
        success=True,
        status="ok",
        data={
            "safe_recipe_ids": [6, 3],
            "excluded_recipe_ids": [5],
            "batch": SimpleNamespace(safe_recipe_ids=[6, 3]),
            "excluded_details": {5: ["p1: 临时约束冲突"]},
        },
    )

    with patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch.object(orchestrator, "_guard_active", return_value=None):
        res = orchestrator._node_audit_health(state)

    # 必须进入终态 no_safe_menu，不能仅凭历史通过而跳过本轮，也不能换菜推荐
    assert res.get("is_terminal") is True
    wf: WorkflowState = res["workflow_state"]
    assert wf.status == RequestStatus.NO_SAFE_MENU


# ============================================================================
# 5. R5 结构化澄清事项持久化与下一轮选项执行回归测试
# ============================================================================


def _create_test_c4_service() -> Any:
    """构建用于测试的内存态 ContextService。"""
    from food_agent_v2.c4 import ContextService

    c4 = ContextService(
        permanent_constraint_loader=SimpleNamespace(load=lambda *args, **kwargs: []),
        memory_source=SimpleNamespace(
            load_session=lambda sid: None,
            load_menu_versions=lambda sid: [],
            save_session=lambda *args, **kwargs: None,
            load_committed_events=lambda sid: [],
            is_clarification_committed=lambda sid, qid: False,
        ),
    )
    pending_store: dict[str, list[dict]] = {}
    fake_redis = SimpleNamespace(
        save_session_state=lambda *args, **kwargs: None,
        load_session_state=lambda *args, **kwargs: None,
        save_constraints=lambda *args, **kwargs: None,
        load_constraints=lambda *args, **kwargs: [],
        save_events=lambda *args, **kwargs: None,
        load_events=lambda *args, **kwargs: [],
        save_menu_history=lambda *args, **kwargs: None,
        load_menu_history=lambda *args, **kwargs: [],
        save_pending_clarifications=lambda sid, items: pending_store.__setitem__(sid, list(items)),
        load_pending_clarifications=lambda sid: list(pending_store.get(sid, [])),
        is_session_lock_held_by=lambda *args, **kwargs: True,
    )
    c4._redis = fake_redis
    return c4


def test_r5_multi_turn_clarification_option_selection_success() -> None:
    """R5 回归：
    Turn 1: 因约束无法成单进入追问，C4 保存结构化澄清选项与 QueryPlan 快照；
    Turn 2: 用户回复'选第二个'，成功消费待澄清事项，只应用该选项允许的约束改动（菜数 4->3），
            严格保留原参与者 (['p1', 'p2'])，最终成单并提交。
    """
    orchestrator = LangGraphRecommendationOrchestrator(llm=DeterministicScriptedAgentModel())
    c4 = _create_test_c4_service()
    session_id = "sess_r5_multiturn"
    build_id = "00000000-0000-0000-0000-000000000001"

    # ---- Turn 1: 首次请求，因时间超限进入追问 ----
    rid1 = _fresh_rid()
    wf_state1 = WorkflowState(
        request_id=rid1,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1", "p2"],
    )
    tool_ctx1 = ToolContext(
        request_id=rid1,
        build_id=build_id,
        session_id=session_id,
        participant_user_mapping={"p1": 1, "p2": 2},
    )

    state_turn1: DietAgentState = {
        "request_id": rid1,
        "session_id": session_id,
        "message": "我要4个菜，15分钟内搞定",
        "participant_refs": ["p1", "p2"],
        "user_id_mapping": {"p1": 1, "p2": 2},
        "build_id": build_id,
        "lock_token": "token-turn1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state1,
        "tool_context": tool_ctx1,
        "inquiry_needed": False,
        "is_terminal": False,
    }

    mock_search = ToolResponse(
        success=True,
        data={"candidates": [1, 2, 3, 4, 5], "retrieval_result": None, "total": 5},
    )
    mock_audit = ToolResponse(
        success=True,
        data={
            "safe_recipe_ids": [1, 2, 3, 4, 5],
            "excluded_recipe_ids": [],
            "batch": None,
            "excluded_details": {},
        },
    )
    mock_combine_fail = ToolResponse(
        success=False,
        error_code="TIME_LIMIT_EXCEEDED",
        diagnostics={
            "bottleneck": "TIME_LIMIT_EXCEEDED",
            "requested_time_minutes": 15,
            "min_needed_minutes": 25,
            "requested_dish_count": 4,
        },
    )

    finalized_states1: list[WorkflowState] = []
    published_events1: list[dict] = []

    with patch(
        "food_agent_v2.c3.graph_orchestrator.search_candidates",
        return_value=mock_search,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu",
        return_value=mock_combine_fail,
    ), patch.object(
        orchestrator, "_guard_active", return_value=None
    ), patch.object(
        orchestrator,
        "_finalize",
        side_effect=lambda st, rid, *args, **kwargs: (
            finalized_states1.append(st),
            kwargs.get("clarification_transition") and d1_api.publish_clarification_event(
                rid,
                dict(
                    kwargs["clarification_transition"].next_public_payload,
                    question_id=kwargs["clarification_transition"].next_question_id,
                ),
            ),
            "needs_clarification",
        )[2],
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize",
        return_value=SimpleNamespace(
            retrieval_query="4个菜 15分钟",
            meal_types=(),
            population_tags=(),
            scenario_tags=(),
            dish_count=4,
            taste_tags=(),
            cuisine_tags=(),
            dish_types=(),
            include_ingredients=(),
            exclude_ingredients=(),
            nutrition_goal_codes=(),
            health_constraints=(),
            time_constraint_seconds=900,
            max_time_minutes=15,
        ),
    ), patch(
        "food_agent_v2.d1.api.publish_clarification_event",
        side_effect=lambda rid, query_plan: published_events1.append(query_plan),
    ):
        orchestrator._graph.invoke(state_turn1)

    # 验证 Turn 1: 产生 needs_clarification
    assert len(finalized_states1) == 1
    assert finalized_states1[0].status == RequestStatus.NEEDS_CLARIFICATION

    # 验证 C4 待澄清持久化：question_id、options、原 query_plan_snapshot
    pending = c4.get_pending_clarifications(session_id)
    assert len(pending) == 1
    item = pending[0]
    assert item["status"] == "pending"
    assert item["diagnosis_code"] == "TIME_LIMIT_EXCEEDED"
    assert len(item["options"]) == 3
    # 选项 2 为减少菜品到 3 道菜
    assert item["options"][1]["option_id"] == 2
    assert item["option_modifications"][2] == {"dish_count_requested": 3}
    assert item["query_plan_snapshot"]["dish_count_requested"] == 4
    assert item["query_plan_snapshot"]["participant_refs"] == ["p1", "p2"]

    # 验证 D1 SSE 发布了结构化选项和 question_id
    assert len(published_events1) == 1
    assert published_events1[0]["question_id"] == item["question_id"]
    assert len(published_events1[0]["options"]) == 3

    # ---- Turn 2: 用户回复'选第二个' ----
    rid2 = _fresh_rid()
    wf_state2 = WorkflowState(
        request_id=rid2,
        build_id=build_id,
        status=RequestStatus.RUNNING,
        participant_refs=["p1", "p2"],
    )
    tool_ctx2 = ToolContext(
        request_id=rid2,
        build_id=build_id,
        session_id=session_id,
        participant_user_mapping={"p1": 1, "p2": 2},
    )

    state_turn2: DietAgentState = {
        "request_id": rid2,
        "session_id": session_id,
        "message": "选第二个",
        "participant_refs": ["p1", "p2"],
        "user_id_mapping": {"p1": 1, "p2": 2},
        "build_id": build_id,
        "lock_token": "token-turn2",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state2,
        "tool_context": tool_ctx2,
        "inquiry_needed": False,
        "is_terminal": False,
    }

    # 模拟 Turn 2 combine_nutritional_menu：当菜数为 3 时规划成功
    actual_dish_counts_called = []

    def fake_combine_menu(**kwargs):
        cnt = kwargs.get("dish_count")
        actual_dish_counts_called.append(cnt)
        if cnt == 3:
            return ToolResponse(
                success=True,
                status="ok",
                data={
                    "plans": [
                        FeasibleMenu(
                            plan_id="plan-turn2",
                            recipe_ids=[1, 2, 3],
                            estimated_makespan_seconds=700,
                            total_score=85.0,
                            menu_hash=menu_hash_for("plan-turn2", [1, 2, 3]),
                            dominant_objective="balanced",
                        )
                    ]
                },
            )
        return mock_combine_fail

    mock_fv = FinalValidationArtifact(
        artifact_id=UUID("30000000-0000-0000-0000-000000000001"),
        request_id=UUID(rid2),
        plan_id="plan-turn2",
        menu_artifact_ref="feasible:1",
        participant_refs=("p1", "p2"),
        recipe_ids=(1, 2, 3),
        participant_recipe_results=(),
        menu_hash=menu_hash_for("plan-turn2", [1, 2, 3]),
        input_fingerprint="b" * 64,
        status="PASS",
    )
    mock_handler = MagicMock()
    def validate_turn2(tool, args):
        assert tool == "validate_selected_menu_health"
        assert args == {"plan_id": mock_fv.plan_id, "recipe_ids": list(mock_fv.recipe_ids)}
        tool_ctx2.previous_results["final_validation"] = mock_fv
        return _validation_tool_result(mock_fv)

    mock_handler.execute.side_effect = validate_turn2

    finalized_states2: list[WorkflowState] = []
    def finalize_turn2(st, *args, **kwargs):
        finalized_states2.append(st)
        return st.status.value

    with patch(
        "food_agent_v2.c3.graph_orchestrator.search_candidates",
        return_value=mock_search,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.audit_recipe_health",
        return_value=mock_audit,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu",
        side_effect=fake_combine_menu,
    ), patch.object(
        orchestrator, "_guard_active", return_value=None
    ), patch.object(
        orchestrator,
        "_finalize",
        side_effect=finalize_turn2,
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.ToolHandler",
        return_value=mock_handler,
    ), patch.object(
        orchestrator,
        "_build_dual_artifacts",
        return_value=(wf_state2, SimpleNamespace(artifact_id=uuid.uuid4())),
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.AuthoritativeAnswerBuilder.build",
        return_value=SimpleNamespace(),
    ):
        orchestrator._graph.invoke(state_turn2)

    # 验证 Turn 2 规划菜数调整为 3，保留原参与者
    assert actual_dish_counts_called == [3]
    assert len(finalized_states2) == 1
    final_wf2 = finalized_states2[0]
    assert final_wf2.status == RequestStatus.COMPLETED
    assert final_wf2.participant_refs == ["p1", "p2"]

    # 验证 C4 中的待澄清事项已被正确消费（状态变为 resolved 或移除出 pending）
    active_pending_after = [
        p for p in c4.get_pending_clarifications(session_id) if p.get("status") == "pending"
    ]
    assert len(active_pending_after) == 0


def test_r5_option_out_of_range_and_ambiguous_reply_re_clarifies() -> None:
    """R5 回归：选项越界、歧义回复或已过期时，不执行错误规划，重新进行澄清。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    c4 = _create_test_c4_service()
    session_id = "sess_r5_edge"
    build_id = "00000000-0000-0000-0000-000000000001"

    # 先存入一个拥有 3 个选项的待澄清事项
    c4.store_pending_clarification(
        session_id,
        {
            "question_id": "q-123",
            "diagnosis_code": "TIME_LIMIT_EXCEEDED",
            "inquiry_reason": "制作时间超限",
            "options": [
                {"option_id": 1, "text": "放宽时间至30分钟", "modifications": {}},
                {"option_id": 2, "text": "减少为3道菜", "modifications": {}},
                {"option_id": 3, "text": "取消时间限制", "modifications": {}},
            ],
            "query_plan_snapshot": {"dish_count_requested": 4, "participant_refs": ["p1"]},
            "status": "pending",
            "expires_at": 9999999999,
        },
    )

    # 用例 1：越界回复 "选第5个" (仅 1-3)
    rid_out = _fresh_rid()
    state_out: DietAgentState = {
        "request_id": rid_out,
        "session_id": session_id,
        "message": "选第5个",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": build_id,
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": WorkflowState(request_id=rid_out, build_id=build_id, status=RequestStatus.RUNNING, participant_refs=["p1"]),
        "tool_context": ToolContext(request_id=rid_out, build_id=build_id),
    }

    res_out = orchestrator._node_understand_intent(state_out)
    assert res_out.get("inquiry_needed") is True
    assert res_out.get("diagnosis_code") == "OPTION_OUT_OF_RANGE"
    assert "超出范围" in res_out.get("inquiry_reason", "")

    # 用例 2：歧义回复 "随便，都行"
    rid_amb = _fresh_rid()
    state_amb: DietAgentState = {
        "request_id": rid_amb,
        "session_id": session_id,
        "message": "两个都行，随便",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": build_id,
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": WorkflowState(request_id=rid_amb, build_id=build_id, status=RequestStatus.RUNNING, participant_refs=["p1"]),
        "tool_context": ToolContext(request_id=rid_amb, build_id=build_id),
    }

    res_amb = orchestrator._node_understand_intent(state_amb)
    assert res_amb.get("inquiry_needed") is True
    assert res_amb.get("diagnosis_code") == "AMBIGUOUS_OPTION_SELECTION"
    assert "未能确定" in res_amb.get("inquiry_reason", "")

    # 用例 3：选项已过期
    c4.clear_pending_clarifications(session_id)
    c4.store_pending_clarification(
        session_id,
        {
            "question_id": "q-expired",
            "diagnosis_code": "TIME_LIMIT_EXCEEDED",
            "inquiry_reason": "制作时间超限",
            "options": [
                {"option_id": 1, "text": "放宽时间至30分钟", "modifications": {}},
            ],
            "query_plan_snapshot": {},
            "status": "pending",
            "expires_at": 1000.0,  # 过去时间
        },
    )
    rid_exp = _fresh_rid()
    state_exp: DietAgentState = {
        "request_id": rid_exp,
        "session_id": session_id,
        "message": "选第一个",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": build_id,
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": WorkflowState(request_id=rid_exp, build_id=build_id, status=RequestStatus.RUNNING, participant_refs=["p1"]),
        "tool_context": ToolContext(request_id=rid_exp, build_id=build_id),
    }

    res_exp = orchestrator._node_understand_intent(state_exp)
    assert res_exp.get("inquiry_needed") is True
    assert res_exp.get("diagnosis_code") == "CLARIFICATION_EXPIRED"
    assert "已过期" in res_exp.get("inquiry_reason", "")


def test_r5_cross_session_and_duplicate_reply_isolation() -> None:
    """R5 回归：跨会话不共享待澄清选项，消费后重复消息不重复触发旧提议。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    c4 = _create_test_c4_service()
    build_id = "00000000-0000-0000-0000-000000000001"

    # 会话 A 存储待澄清事项
    c4.store_pending_clarification(
        "sess_A",
        {
            "question_id": "q-A",
            "diagnosis_code": "TIME_LIMIT_EXCEEDED",
            "inquiry_reason": "A 会话超限",
            "options": [
                {"option_id": 1, "text": "选项 1", "modifications": {"dish_count_requested": 3}},
            ],
            "query_plan_snapshot": {"dish_count_requested": 4, "participant_refs": ["p1"]},
            "status": "pending",
            "expires_at": 9999999999,
        },
    )

    # 会话 B 没有待澄清事项；发送 "选第一个" 不得关联到会话 A
    rid_b = _fresh_rid()
    state_b: DietAgentState = {
        "request_id": rid_b,
        "session_id": "sess_B",
        "message": "选第一个",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": build_id,
        "lock_token": "token-b",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": WorkflowState(request_id=rid_b, build_id=build_id, status=RequestStatus.RUNNING, participant_refs=["p1"]),
        "tool_context": ToolContext(request_id=rid_b, build_id=build_id),
    }

    with patch(
        "food_agent_v2.c3.graph_orchestrator.FastIntentRouter.route",
        return_value=IntentDelta(intent="new_recommendation", query="选第一个"),
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize",
        return_value=SimpleNamespace(
            retrieval_query="选第一个",
            meal_types=(),
            population_tags=(),
            scenario_tags=(),
            dish_count=None,
            taste_tags=(),
            cuisine_tags=(),
            dish_types=(),
            include_ingredients=(),
            exclude_ingredients=(),
            nutrition_goal_codes=(),
            health_constraints=(),
            time_constraint_seconds=None,
            max_time_minutes=None,
        ),
    ):
        res_b = orchestrator._node_understand_intent(state_b)

    # 会话 B 未被会话 A 的待澄清篡改，正常走 new_recommendation
    assert res_b.get("query_plan").dish_count_requested is None
    # 会话 A 的待澄清未被会话 B 消费
    assert len(c4.get_pending_clarifications("sess_A")) == 1

    # 会话 A 消费选项
    c4.consume_pending_clarification("sess_A", "q-A")
    assert len([p for p in c4.get_pending_clarifications("sess_A") if p.get("status") == "pending"]) == 0

    # 会话 A 重复发送 "选第一个"，由于待澄清已解决，不会再次触发该选项的旧提议
    rid_a_dup = _fresh_rid()
    state_a_dup: DietAgentState = {
        "request_id": rid_a_dup,
        "session_id": "sess_A",
        "message": "选第一个",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": build_id,
        "lock_token": "token-a2",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": WorkflowState(request_id=rid_a_dup, build_id=build_id, status=RequestStatus.RUNNING, participant_refs=["p1"]),
        "tool_context": ToolContext(request_id=rid_a_dup, build_id=build_id),
    }

    with patch(
        "food_agent_v2.c3.graph_orchestrator.FastIntentRouter.route",
        return_value=IntentDelta(intent="new_recommendation", query="选第一个"),
    ), patch(
        "food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize",
        return_value=SimpleNamespace(
            retrieval_query="选第一个",
            meal_types=(),
            population_tags=(),
            scenario_tags=(),
            dish_count=None,
            taste_tags=(),
            cuisine_tags=(),
            dish_types=(),
            include_ingredients=(),
            exclude_ingredients=(),
            nutrition_goal_codes=(),
            health_constraints=(),
            time_constraint_seconds=None,
            max_time_minutes=None,
        ),
    ):
        res_a_dup = orchestrator._node_understand_intent(state_a_dup)

    # 验证没有残留关联到之前的 3 道菜提议
    assert res_a_dup.get("query_plan").dish_count_requested is None


# ============================================================================
# 8. R6: Agent 行动—工具反馈循环与安全门卫测试
# ============================================================================


def test_r6_scripted_model_clarify_immediately():
    """R6: 脚本化模型选择'先澄清'，1次决策，0次工具执行，生成 needs_clarification 终态。"""
    class ScriptedClarifyModel:
        def decide_action(self, state: DietAgentState) -> AgentAction:
            return AgentAction(
                action=ActionType.ASK_USER,
                arguments={
                    "inquiry_category": "TASTE_PREFERENCE_MISSING",
                    "reason": "请问您偏好辣还是清淡口味？",
                    "options": [
                        {"option_id": 1, "text": "选项 1: 清淡粤菜/家常菜", "modifications": {}},
                        {"option_id": 2, "text": "选项 2: 香辣川湘菜", "modifications": {}},
                    ],
                },
                summary="用户偏好信息缺失，主动发起澄清",
            )

    orchestrator = LangGraphRecommendationOrchestrator(llm=ScriptedClarifyModel())
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_clarify",
        "message": "随便推荐点吃的",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    finalized_states: list[WorkflowState] = []
    with patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="随便推荐", meal_types=(), population_tags=(), scenario_tags=(), dish_count=None,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.NEEDS_CLARIFICATION
    assert "请问您偏好辣还是清淡口味？" in finalized_states[0].error.message
    # 检查 C4 中保存了该待澄清
    pending = c4.get_pending_clarifications("sess_clarify")
    assert len(pending) == 1
    assert pending[0]["diagnosis_code"] == "TASTE_PREFERENCE_MISSING"


def test_v2_model_clarification_carries_session_revision_to_commit() -> None:
    """A first-turn model ask_user must commit against the v2 revision it read."""
    model = MagicMock()
    del model.decide_action
    model.invoke.return_value = {"content": (
        '{"action":"ask_user","arguments":{"inquiry_category":"constraint_conflict",'
        '"reason":"三道菜无法在一分钟内完成，请选择调整方式",'
        '"options":['
        '{"option_id":1,"text":"放宽时间","modifications":{"time_constraint_seconds":1800}},'
        '{"option_id":2,"text":"减少菜数","modifications":{"dish_count_requested":1}}'
        ']},"evidence_refs":[],"summary":"先让用户选择"}'
    )}
    orchestrator = LangGraphRecommendationOrchestrator(llm=model)
    c4 = _create_test_c4_service()
    c4.load_clarification_state = lambda _sid: {
        "protocol_version": "v2", "clarification_revision": 0,
    }
    request_id = _fresh_rid()
    state: DietAgentState = {
        "request_id": request_id,
        "session_id": "sess_v2_model_clarify",
        "message": "推荐三道晚餐，所有菜品合计必须在1分钟内完成。如果无法满足，请先让我选择放宽时间或者减少菜数。",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "00000000-0000-0000-0000-000000000001",
        "lock_token": "458",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": WorkflowState(
            request_id=request_id, build_id="00000000-0000-0000-0000-000000000001",
            status=RequestStatus.RUNNING, participant_refs=["p1"],
        ),
        "tool_context": ToolContext(
            request_id=request_id, build_id="00000000-0000-0000-0000-000000000001",
        ),
    }
    transitions = []
    with patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda _st, _rid, _c4, _token, **kw: transitions.append(kw["clarification_transition"]) or "needs_clarification"), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道晚餐", meal_types=("dinner",), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=60, max_time_minutes=1,
         )):
        orchestrator._graph.invoke(state)

    assert len(transitions) == 1
    assert transitions[0].expected_revision == 0
    assert transitions[0].next_question_id == f"q_{request_id}"


def test_r6_scripted_model_normal_recommendation_trajectory():
    """R6: 脚本化模型选择'正常推荐'链路：检索 -> 审核 -> 规划 -> 校验 -> 完成，
    断言每轮观察事实准确传递给下一次决策，工具执行次数与决策轮次符合预期。"""
    class ScriptedNormalModel:
        def __init__(self):
            self.turn = 0
            self.recorded_observations: list[list[Observation]] = []

        def decide_action(self, state: DietAgentState) -> AgentAction:
            self.turn += 1
            obs = list(state.get("observations") or [])
            self.recorded_observations.append(obs)

            if self.turn == 1:
                assert len(obs) == 0
                return AgentAction(action=ActionType.SEARCH_CANDIDATES, arguments={"query": "清淡家常菜"})
            elif self.turn == 2:
                assert len(obs) == 1
                assert obs[0].action == ActionType.SEARCH_CANDIDATES
                assert obs[0].desensitized_facts["candidate_count"] == 3
                return AgentAction(
                    action=ActionType.AUDIT_RECIPE_HEALTH,
                    arguments={"candidate_recipe_ids": [10, 20, 30]},
                    evidence_refs=[obs[0].evidence_ref],
                )
            elif self.turn == 3:
                assert len(obs) == 2
                assert obs[1].action == ActionType.AUDIT_RECIPE_HEALTH
                assert obs[1].desensitized_facts["safe_count"] == 3
                return AgentAction(
                    action=ActionType.COMBINE_NUTRITIONAL_MENU,
                    arguments={"dish_count": 3},
                    evidence_refs=[obs[1].evidence_ref],
                )
            elif self.turn == 4:
                assert len(obs) == 3
                assert obs[2].action == ActionType.COMBINE_NUTRITIONAL_MENU
                assert obs[2].desensitized_facts["feasible_plan_count"] == 1
                return AgentAction(
                    action=ActionType.VALIDATE_SELECTED_MENU,
                    arguments={"plan_id": "plan-scripted-1", "recipe_ids": [10, 20, 30]},
                    evidence_refs=[obs[2].evidence_ref],
                )
            elif self.turn == 5:
                assert len(obs) == 4
                assert obs[3].action == ActionType.VALIDATE_SELECTED_MENU
                assert obs[3].desensitized_facts["validation_status"] == "PASS"
                return AgentAction(
                    action=ActionType.FINISH,
                    arguments={"plan_id": "plan-scripted-1", "final_validation_ref": obs[3].evidence_ref},
                    evidence_refs=[obs[3].evidence_ref],
                )
            raise RuntimeError(f"Unexpected turn {self.turn}")

    model = ScriptedNormalModel()
    orchestrator = LangGraphRecommendationOrchestrator(llm=model)
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_normal",
        "message": "推荐三道清淡家常菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    mock_search = ToolResponse(success=True, data={"candidates": [10, 20, 30], "total": 3})
    mock_audit = ToolResponse(
        success=True,
        data={
            "safe_recipe_ids": [10, 20, 30],
            "excluded_recipe_ids": [],
            "batch": SimpleNamespace(safe_recipe_ids=[10, 20, 30]),
            "excluded_details": {},
        },
    )
    mock_plan = FeasibleMenu(
        plan_id="plan-scripted-1",
        recipe_ids=[10, 20, 30],
        menu_hash=menu_hash_for("plan-scripted-1", [10, 20, 30]),
        dominant_objective="balanced",
        total_score=90.0,
        estimated_makespan_seconds=1200,
    )
    mock_combine = ToolResponse(success=True, data={"plans": [mock_plan], "count": 1})

    finalized_states: list[WorkflowState] = []
    with patch("food_agent_v2.c3.graph_orchestrator.search_candidates", return_value=mock_search), \
         patch("food_agent_v2.c3.graph_orchestrator.audit_recipe_health", return_value=mock_audit), \
         patch("food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu", return_value=mock_combine), \
         patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(ToolHandler, "execute", autospec=True, side_effect=_execute_with_bound_final_validation), \
         patch.object(orchestrator, "_select_validate_answer", side_effect=lambda st, ctx, plans, *a, **k: reduce_workflow_state(st, action="unified_review", status="PASS")), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道清淡家常菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.COMPLETED
    assert model.turn == 5
    assert len(model.recorded_observations) == 5


def test_r6_scripted_model_expand_candidates_and_replan_trajectory():
    """R6: 脚本化模型选择'补充搜索后再规划'：
    检索(2候选) -> 审核(2安全) -> 规划(缺额报错) -> 扩展召回(新增3候选) -> 重新审核(5安全) -> 重新规划(成功) -> 校验 -> 完成。
    验证真实轨迹中包含 EXPAND_CANDIDATES，且多轮观察链闭环。"""
    class ScriptedExpandModel:
        def __init__(self):
            self.turn = 0
            self.actions_taken: list[ActionType] = []

        def decide_action(self, state: DietAgentState) -> AgentAction:
            self.turn += 1
            obs = list(state.get("observations") or [])
            if self.turn == 1:
                act = AgentAction(action=ActionType.SEARCH_CANDIDATES, arguments={"query": "粤菜"})
            elif self.turn == 2:
                act = AgentAction(
                    action=ActionType.AUDIT_RECIPE_HEALTH,
                    arguments={"candidate_recipe_ids": [1, 2]},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 3:
                act = AgentAction(
                    action=ActionType.COMBINE_NUTRITIONAL_MENU,
                    arguments={"dish_count": 4},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 4:
                # combine 返回 no_solution (缺额)，Agent 自主决定补充搜索
                assert obs[-1].status == "no_solution"
                act = AgentAction(
                    action=ActionType.EXPAND_CANDIDATES,
                    arguments={"query": "家常菜 补充", "retrieval_evidence_ref": obs[0].evidence_ref},
                    evidence_refs=[obs[0].evidence_ref],
                )
            elif self.turn == 5:
                # 扩展召回成功后，对扩充后的候选集合重新审核
                assert obs[-1].status == "ok"
                act = AgentAction(
                    action=ActionType.AUDIT_RECIPE_HEALTH,
                    arguments={"candidate_recipe_ids": [1, 2, 3, 4, 5]},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 6:
                act = AgentAction(
                    action=ActionType.COMBINE_NUTRITIONAL_MENU,
                    arguments={"dish_count": 4},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 7:
                act = AgentAction(
                    action=ActionType.VALIDATE_SELECTED_MENU,
                    arguments={"plan_id": "plan-expanded-4", "recipe_ids": [1, 2, 3, 4]},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 8:
                act = AgentAction(
                    action=ActionType.FINISH,
                    arguments={"plan_id": "plan-expanded-4", "final_validation_ref": obs[-1].evidence_ref},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            else:
                raise RuntimeError(f"Unexpected turn {self.turn}")
            self.actions_taken.append(act.action)
            return act

    model = ScriptedExpandModel()
    orchestrator = LangGraphRecommendationOrchestrator(llm=model)
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_expand",
        "message": "我要4个菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    # 模拟工具执行：第 1 次搜索 2 候选，第 2 次扩展搜索 3 候选
    search_call_count = 0
    def mock_search_fn(*args, **kwargs):
        nonlocal search_call_count
        search_call_count += 1
        if search_call_count == 1:
            return ToolResponse(success=True, data={"candidates": [1, 2], "total": 2})
        return ToolResponse(success=True, data={"candidates": [3, 4, 5], "total": 3})

    audit_call_count = 0
    def mock_audit_fn(*args, **kwargs):
        nonlocal audit_call_count
        audit_call_count += 1
        cids = kwargs.get("candidate_recipe_ids", [])
        return ToolResponse(
            success=True,
            data={
                "safe_recipe_ids": list(cids),
                "excluded_recipe_ids": [],
                "batch": SimpleNamespace(safe_recipe_ids=list(cids)),
                "excluded_details": {},
            },
        )

    combine_call_count = 0
    def mock_combine_fn(*args, **kwargs):
        nonlocal combine_call_count
        combine_call_count += 1
        if combine_call_count == 1:
            return ToolResponse(
                success=False,
                error_code="SAFE_CANDIDATE_SHORTAGE",
                diagnostics={"safe_count": 2, "requested_count": 4, "deficit": 2},
            )
        plan = FeasibleMenu(
            plan_id="plan-expanded-4",
            recipe_ids=[1, 2, 3, 4],
            menu_hash=menu_hash_for("plan-expanded-4", [1, 2, 3, 4]),
            dominant_objective="balanced",
            total_score=92.0,
            estimated_makespan_seconds=1200,
        )
        return ToolResponse(success=True, data={"plans": [plan], "count": 1})

    finalized_states: list[WorkflowState] = []
    with patch("food_agent_v2.c3.graph_orchestrator.search_candidates", side_effect=mock_search_fn), \
         patch("food_agent_v2.c3.graph_orchestrator.audit_recipe_health", side_effect=mock_audit_fn), \
         patch("food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu", side_effect=mock_combine_fn), \
         patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(ToolHandler, "execute", autospec=True, side_effect=_execute_with_bound_final_validation), \
         patch.object(orchestrator, "_select_validate_answer", side_effect=lambda st, ctx, plans, *a, **k: reduce_workflow_state(st, action="unified_review", status="PASS")), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="4个菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=4,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        final_dict = orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.COMPLETED
    assert ActionType.EXPAND_CANDIDATES in model.actions_taken
    assert model.turn == 8

    # D5: 断言 policy 实际计数，确保文档数字来自执行结果
    policy = final_dict["policy"]
    assert policy.decision_count == 8
    assert policy.tool_execution_count == 7
    assert policy.expansion_count == 1
    assert policy.health_revision_count == 0
    assert policy.combine_count == 2


def test_r6_gate_rejects_premature_finish_without_final_validation():
    """R6 安全门卫：模型未调用 validate_selected_menu 企图直接 finish，门卫拒绝并 fail-closed (INV-007)。"""
    class PrematureFinishModel:
        def decide_action(self, state: DietAgentState) -> AgentAction:
            # 企图直接调用 finish
            return AgentAction(
                action=ActionType.FINISH,
                arguments={"plan_id": "plan-unvalidated-1", "final_validation_ref": "fake_ref"},
                evidence_refs=["fake_ref"],
            )

    orchestrator = LangGraphRecommendationOrchestrator(llm=PrematureFinishModel())
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_premature",
        "message": "推荐三道菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    finalized_states: list[WorkflowState] = []
    with patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.FAILED
    assert finalized_states[0].error.error_code in ("FINAL_HEALTH_VALIDATION_MISSING", "INVALID_EVIDENCE_REFERENCE")


def test_r6_gate_rejects_forged_evidence_reference():
    """R6 安全门卫：模型伪造不存在的 evidence_ref，门卫校验失败，拒绝执行并 fail-closed。"""
    class ForgedEvidenceModel:
        def decide_action(self, state: DietAgentState) -> AgentAction:
            return AgentAction(
                action=ActionType.SEARCH_CANDIDATES,
                arguments={"query": "家常菜"},
                evidence_refs=["forged_fake_evidence_ref_999"],
            )

    orchestrator = LangGraphRecommendationOrchestrator(llm=ForgedEvidenceModel())
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_forged",
        "message": "推荐三道菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    finalized_states: list[WorkflowState] = []
    with patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.FAILED
    assert finalized_states[0].error.error_code == "INVALID_EVIDENCE_REFERENCE"


def test_r6_gate_rejects_unauthorized_action():
    """R6 安全门卫：模型输出白名单外的非法动作，门卫直接拦截并 fail-closed。"""
    class UnauthorizedActionModel:
        def decide_action(self, state: DietAgentState) -> dict:
            return {
                "action": "delete_patient_health_records",
                "arguments": {"user_id": 1},
            }

    orchestrator = LangGraphRecommendationOrchestrator(llm=UnauthorizedActionModel())
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_unauthorized",
        "message": "推荐三道菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    finalized_states: list[WorkflowState] = []
    with patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.FAILED
    assert finalized_states[0].error.error_code == "MODEL_ACTION_SCHEMA_INVALID"


def test_r6_gate_rejects_unauthorized_recipe_ids_in_audit():
    """R6 安全门卫：模型在健康审核中传入未经检索的外部伪造菜品 ID，门卫校验失败拦截。"""
    class InjectRecipesModel:
        def __init__(self):
            self.turn = 0
        def decide_action(self, state: DietAgentState) -> AgentAction:
            self.turn += 1
            if self.turn == 1:
                return AgentAction(action=ActionType.SEARCH_CANDIDATES, arguments={"query": "家常菜"})
            return AgentAction(
                action=ActionType.AUDIT_RECIPE_HEALTH,
                arguments={"candidate_recipe_ids": [1, 2, 99999]},
                evidence_refs=[state["observations"][0].evidence_ref],
            )

    orchestrator = LangGraphRecommendationOrchestrator(llm=InjectRecipesModel())
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_audit_unauth",
        "message": "推荐三道菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    mock_search = ToolResponse(success=True, data={"candidates": [1, 2, 3], "total": 3})
    finalized_states: list[WorkflowState] = []
    with patch("food_agent_v2.c3.graph_orchestrator.search_candidates", return_value=mock_search), \
         patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.FAILED
    assert finalized_states[0].error.error_code == "UNAUTHORIZED_RECIPE_IDS"


def test_r6_gate_rejects_duplicate_tool_call():
    """R6 安全门卫：模型连续两次调用相同参数的同一工具，门卫判定重复执行，终止执行。"""
    class DuplicateSearchModel:
        def decide_action(self, state: DietAgentState) -> AgentAction:
            # 始终重复调用相同的 search
            return AgentAction(action=ActionType.SEARCH_CANDIDATES, arguments={"query": "家常菜"})

    orchestrator = LangGraphRecommendationOrchestrator(llm=DuplicateSearchModel())
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_dup",
        "message": "推荐三道菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    mock_search = ToolResponse(success=True, data={"candidates": [1, 2, 3], "total": 3})
    finalized_states: list[WorkflowState] = []
    with patch("food_agent_v2.c3.graph_orchestrator.search_candidates", return_value=mock_search), \
         patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.FAILED
    assert finalized_states[0].error.error_code == "DUPLICATE_TOOL_CALL"


def test_r6_gate_rejects_budget_exhaustion():
    """R6 安全门卫：决策或工具预算耗尽时强制停止，防止循环失控。"""
    # 1. 决策预算耗尽
    policy_tight = AgentPolicy(max_decisions=2)
    assert policy_tight.check_decision_budget().allowed is True
    assert policy_tight.check_decision_budget().allowed is True
    res = policy_tight.check_decision_budget()
    assert res.allowed is False
    assert res.error_code == "DECISION_BUDGET_EXCEEDED"

    # 2. 工具预算耗尽
    policy_tool = AgentPolicy(max_tool_executions=1)
    act1 = AgentAction(action=ActionType.SEARCH_CANDIDATES, arguments={"query": "q1"})
    policy_tool.record_tool_execution(act1)
    act2 = AgentAction(action=ActionType.SEARCH_CANDIDATES, arguments={"query": "q2"})
    res2 = policy_tool.validate_action_gate(act2, {})
    assert res2.allowed is False
    assert res2.error_code == "TOOL_BUDGET_EXCEEDED"

    # 3. 补充召回预算耗尽（上限 1 次）
    policy_exp = AgentPolicy(max_expansions=1)
    exp1 = AgentAction(action=ActionType.EXPAND_CANDIDATES, arguments={"query": "e1"})
    policy_exp.record_tool_execution(exp1)
    exp2 = AgentAction(action=ActionType.EXPAND_CANDIDATES, arguments={"query": "e2"})
    res3 = policy_exp.validate_action_gate(exp2, {})
    assert res3.allowed is False
    assert res3.error_code == "EXPANSION_BUDGET_EXCEEDED"


# ============================================================================
# 9. Section 13 D1 & D2: 模型失败显式 Fail-Closed 与预算语义解耦测试
# ============================================================================


def test_d1_model_not_configured_fails_closed():
    """D1: 模型未配置 (llm is None) 时显式失败，严禁静默回退至确定性降级。"""
    orchestrator = LangGraphRecommendationOrchestrator(llm=None)
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_d1_no_model",
        "message": "推荐清淡家常菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    finalized_states: list[WorkflowState] = []
    with patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="清淡家常菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=None,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.FAILED
    assert finalized_states[0].error.error_code == "MODEL_NOT_CONFIGURED"


def test_d1_model_invocation_timeout_fails_closed():
    """D1: 模型调用超时或网络异常时，显式进入失败终态，绝不隐式代跑。"""
    mock_llm = MagicMock()
    mock_llm.invoke.side_effect = TimeoutError("DeepSeek API connection timed out")
    del mock_llm.decide_action  # 确保不是脚本化模型

    orchestrator = LangGraphRecommendationOrchestrator(llm=mock_llm)
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_d1_timeout",
        "message": "推荐三道菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    finalized_states: list[WorkflowState] = []
    with patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.FAILED
    assert finalized_states[0].error.error_code == "MODEL_INVOCATION_FAILED"
    assert "TimeoutError" in finalized_states[0].error.message


def test_d1_model_output_invalid_json_fails_closed():
    """D1: 模型输出非合法 JSON 时显式失败，保留原始错误类别。"""
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = {"content": "抱歉，作为 AI 我建议你吃番茄炒蛋，而不是输出 JSON"}
    del mock_llm.decide_action

    orchestrator = LangGraphRecommendationOrchestrator(llm=mock_llm)
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_d1_invalid_json",
        "message": "推荐三道菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    finalized_states: list[WorkflowState] = []
    with patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.FAILED
    assert finalized_states[0].error.error_code == "MODEL_OUTPUT_INVALID_JSON"


def test_d1_model_output_invalid_schema_fails_closed():
    """D1: 模型输出非法 Action 类型时显式失败。"""
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = {"content": '{"action": "hallucinated_tool", "arguments": {}}'}
    del mock_llm.decide_action

    orchestrator = LangGraphRecommendationOrchestrator(llm=mock_llm)
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_d1_invalid_schema",
        "message": "推荐三道菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    finalized_states: list[WorkflowState] = []
    with patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="三道菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=3,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.FAILED
    assert finalized_states[0].error.error_code == "MODEL_ACTION_SCHEMA_INVALID"


def test_d2_expand_replan_and_health_revision_budget_independence():
    """D2 组合场景：首次规划无解 -> 扩展召回 -> 再次规划 -> 最终健康 EXCLUDE -> 一次健康修订 -> 最终通过。
    验证：
    1. 候选变化后的重新规划不占用健康修订额度；
    2. 健康排除后的修订单独计数且允许一次；
    3. 全局总决策（11次 <= 12）与工具执行（10次 <= 10）严格符合预算。"""
    class ScriptedCombinedModel:
        def __init__(self):
            self.turn = 0
            self.actions_taken: list[ActionType] = []

        def decide_action(self, state: DietAgentState) -> AgentAction:
            self.turn += 1
            obs = list(state.get("observations") or [])
            if self.turn == 1:
                # 1. 搜索
                act = AgentAction(action=ActionType.SEARCH_CANDIDATES, arguments={"query": "家常菜"})
            elif self.turn == 2:
                # 2. 健康审核 (2候选)
                act = AgentAction(
                    action=ActionType.AUDIT_RECIPE_HEALTH,
                    arguments={"candidate_recipe_ids": [1, 2]},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 3:
                # 3. 规划 4 菜 -> 预期缺额
                act = AgentAction(
                    action=ActionType.COMBINE_NUTRITIONAL_MENU,
                    arguments={"dish_count": 4},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 4:
                # 4. 扩展召回
                assert obs[-1].status == "no_solution"
                act = AgentAction(
                    action=ActionType.EXPAND_CANDIDATES,
                    arguments={"query": "家常菜 补充", "retrieval_evidence_ref": obs[0].evidence_ref},
                    evidence_refs=[obs[0].evidence_ref],
                )
            elif self.turn == 5:
                # 5. 重新审核扩展后的候选 (5候选)
                act = AgentAction(
                    action=ActionType.AUDIT_RECIPE_HEALTH,
                    arguments={"candidate_recipe_ids": [1, 2, 3, 4, 5]},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 6:
                # 6. 候选变化后再次规划 (第2次 combine，候选扩充，不应消耗健康修订额度)
                act = AgentAction(
                    action=ActionType.COMBINE_NUTRITIONAL_MENU,
                    arguments={"dish_count": 4},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 7:
                # 7. 最终校验 -> 模拟返回 EXCLUDE
                act = AgentAction(
                    action=ActionType.VALIDATE_SELECTED_MENU,
                    arguments={"plan_id": "plan-pre-revision", "recipe_ids": [1, 2, 3, 4]},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 8:
                # EXCLUDE invalidates the old health and planning evidence.
                assert obs[-1].status == "no_solution"
                assert state["execution_context"]["is_health_revision"] is True
                assert state["execution_context"]["health_evaluated"] is False
                assert not state.get("feasible_menus")
                assert "health_evaluation" not in state["tool_context"].previous_results
                assert "feasible_menus" not in state["tool_context"].previous_results
                act = AgentAction(
                    action=ActionType.AUDIT_RECIPE_HEALTH,
                    arguments={"candidate_recipe_ids": [1, 2, 3, 4, 5]},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 9:
                # A new audit must precede the one permitted health revision.
                assert obs[-1].action == ActionType.AUDIT_RECIPE_HEALTH
                assert state["execution_context"]["is_health_revision"] is True
                assert state["safe_recipe_ids"] == [1, 2, 3, 5]
                act = AgentAction(
                    action=ActionType.COMBINE_NUTRITIONAL_MENU,
                    arguments={"dish_count": 4},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 10:
                assert state["execution_context"]["is_health_revision"] is False
                act = AgentAction(
                    action=ActionType.VALIDATE_SELECTED_MENU,
                    arguments={"plan_id": "plan-post-revision", "recipe_ids": [1, 2, 3, 5]},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            elif self.turn == 11:
                act = AgentAction(
                    action=ActionType.FINISH,
                    arguments={"plan_id": "plan-post-revision", "final_validation_ref": obs[-1].evidence_ref},
                    evidence_refs=[obs[-1].evidence_ref],
                )
            else:
                raise RuntimeError(f"Unexpected turn {self.turn}")
            self.actions_taken.append(act.action)
            return act

    model = ScriptedCombinedModel()
    orchestrator = LangGraphRecommendationOrchestrator(llm=model)
    rid = _fresh_rid()
    wf_state = WorkflowState(request_id=rid, build_id="00000000-0000-0000-0000-000000000001", status=RequestStatus.RUNNING, participant_refs=["p1"])
    tool_ctx = ToolContext(request_id=rid, build_id="00000000-0000-0000-0000-000000000001")
    c4 = _create_test_c4_service()

    state: DietAgentState = {
        "request_id": rid,
        "session_id": "sess_d2_combined",
        "message": "我要4个菜",
        "participant_refs": ["p1"],
        "user_id_mapping": {"p1": 1},
        "build_id": "b1",
        "lock_token": "token-1",
        "lost": SimpleNamespace(is_set=lambda: False),
        "c4": c4,
        "workflow_state": wf_state,
        "tool_context": tool_ctx,
    }

    search_count = 0
    def mock_search_fn(*args, **kwargs):
        nonlocal search_count
        search_count += 1
        if search_count == 1:
            return ToolResponse(success=True, data={"candidates": [1, 2], "total": 2})
        return ToolResponse(success=True, data={"candidates": [3, 4, 5], "total": 3})

    audit_count = 0
    def mock_audit_fn(*args, **kwargs):
        nonlocal audit_count
        audit_count += 1
        cids = kwargs.get("candidate_recipe_ids", [])
        expected_candidates = [1, 2] if audit_count == 1 else [1, 2, 3, 4, 5]
        assert list(cids) == expected_candidates
        excluded = [4] if audit_count == 3 else []
        safe = [recipe_id for recipe_id in cids if recipe_id not in excluded]
        batch = SimpleNamespace(
            safe_recipe_ids=safe,
            excluded_recipe_ids=excluded,
            participant_recipe_results=[],
        )
        return ToolResponse(
            success=True,
            data={
                "safe_recipe_ids": safe,
                "excluded_recipe_ids": excluded,
                "batch": batch,
                "excluded_details": {4: ["new health exclusion"]} if excluded else {},
            },
        )

    combine_count = 0
    def mock_combine_fn(*args, **kwargs):
        nonlocal combine_count
        combine_count += 1
        if combine_count == 1:
            return ToolResponse(
                success=False,
                error_code="SAFE_CANDIDATE_SHORTAGE",
                diagnostics={"safe_count": 2, "requested_count": 4, "deficit": 2},
            )
        elif combine_count == 2:
            plan = FeasibleMenu(
                plan_id="plan-pre-revision",
                recipe_ids=[1, 2, 3, 4],
                menu_hash=menu_hash_for("plan-pre-revision", [1, 2, 3, 4]),
                dominant_objective="balanced",
                total_score=90.0,
                estimated_makespan_seconds=1200,
            )
            return ToolResponse(success=True, data={"plans": [plan], "count": 1})
        else:
            assert audit_count == 3
            assert kwargs["safe_recipe_ids"] == [1, 2, 3, 5]
            plan = FeasibleMenu(
                plan_id="plan-post-revision",
                recipe_ids=[1, 2, 3, 5],
                menu_hash=menu_hash_for("plan-post-revision", [1, 2, 3, 5]),
                dominant_objective="balanced",
                total_score=91.0,
                estimated_makespan_seconds=1150,
            )
            return ToolResponse(success=True, data={"plans": [plan], "count": 1})

    validate_count = 0
    def mock_validate_tool(action_name, kwargs):
        nonlocal validate_count
        assert action_name == "validate_selected_menu_health"
        validate_count += 1
        plan_id = kwargs.get("plan_id")
        recipe_ids = kwargs.get("recipe_ids", [])
        if validate_count == 1:
            # 第一次校验返回 EXCLUDE
            fv = FinalValidationArtifact(
                artifact_id=uuid.uuid4(),
                request_id=UUID(rid),
                plan_id=plan_id,
                menu_artifact_ref=str(uuid.uuid4()),
                participant_refs=("p1",),
                recipe_ids=tuple(recipe_ids),
                participant_recipe_results=(),
                menu_hash=menu_hash_for(plan_id, recipe_ids),
                input_fingerprint="0" * 64,
                status="EXCLUDE",
            )
        else:
            # 修订后校验返回 PASS
            fv = FinalValidationArtifact(
                artifact_id=uuid.uuid4(),
                request_id=UUID(rid),
                plan_id=plan_id,
                menu_artifact_ref=str(uuid.uuid4()),
                participant_refs=("p1",),
                recipe_ids=tuple(recipe_ids),
                participant_recipe_results=(),
                menu_hash=menu_hash_for(plan_id, recipe_ids),
                input_fingerprint="0" * 64,
                status="PASS",
            )
        tool_ctx.previous_results["final_validation"] = fv
        return _validation_tool_result(fv)

    finalized_states: list[WorkflowState] = []
    with patch("food_agent_v2.c3.graph_orchestrator.search_candidates", side_effect=mock_search_fn), \
         patch("food_agent_v2.c3.graph_orchestrator.audit_recipe_health", side_effect=mock_audit_fn), \
         patch("food_agent_v2.c3.graph_orchestrator.combine_nutritional_menu", side_effect=mock_combine_fn), \
         patch.object(ToolHandler, "execute", side_effect=mock_validate_tool), \
         patch.object(orchestrator, "_guard_active", return_value=None), \
         patch.object(orchestrator, "_select_validate_answer", side_effect=lambda st, ctx, plans, *a, **k: reduce_workflow_state(st, action="unified_review", status="PASS")), \
         patch.object(orchestrator, "_finalize", side_effect=lambda st, *args, **kwargs: finalized_states.append(st)), \
         patch("food_agent_v2.c3.graph_orchestrator.QueryNormalizer.normalize", return_value=SimpleNamespace(
             retrieval_query="4个菜", meal_types=(), population_tags=(), scenario_tags=(), dish_count=4,
             taste_tags=(), cuisine_tags=(), dish_types=(), include_ingredients=(), exclude_ingredients=(),
             nutrition_goal_codes=(), health_constraints=(), time_constraint_seconds=None, max_time_minutes=None
         )):
        final_dict = orchestrator._graph.invoke(state)

    assert len(finalized_states) == 1
    assert finalized_states[0].status == RequestStatus.COMPLETED, finalized_states[0].error
    assert model.turn == 11
    assert search_count == 2
    assert audit_count == 3
    assert combine_count == 3
    assert validate_count == 2

    # 验证最终预算消耗
    policy = final_dict["policy"]
    assert policy.decision_count == 11
    assert policy.tool_execution_count == 10
    assert policy.expansion_count == 1
    assert policy.health_revision_count == 1
    assert policy.combine_count == 3


def test_d2_second_health_revision_rejected_by_gate():
    """D2: 第二次健康排除后的重新规划必须被安全门卫拒绝 (HEALTH_REVISION_BUDGET_EXCEEDED)。"""
    policy = AgentPolicy(max_health_revisions=1)
    # 模拟第一次健康修订已执行
    combine_act = AgentAction(
        action=ActionType.COMBINE_NUTRITIONAL_MENU,
        arguments={"dish_count": 4},
        evidence_refs=["ev:rev1"],
    )
    policy.record_tool_execution(combine_act, execution_context={"is_health_revision": True})
    assert policy.health_revision_count == 1

    # 第二次发生健康修订企图
    combine_act2 = AgentAction(
        action=ActionType.COMBINE_NUTRITIONAL_MENU,
        arguments={"dish_count": 4},
        evidence_refs=["ev:rev2"],
    )
    res = policy.validate_action_gate(
        combine_act2,
        execution_context={"is_health_revision": True, "health_evaluated": True},
    )
    assert res.allowed is False
    assert res.error_code == "HEALTH_REVISION_BUDGET_EXCEEDED"
