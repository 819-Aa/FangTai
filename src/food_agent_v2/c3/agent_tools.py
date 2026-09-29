"""Agent 工具适配器与脱敏观察生成（Section 5.2 & Section 9）。"""

from __future__ import annotations

import logging
from typing import Any

from food_agent_v2.c3.agent_actions import ActionType, AgentAction, Observation
from food_agent_v2.c3.tool_handler import ToolContext, ToolHandler
from food_agent_v2.c3.tools import (
    audit_recipe_health,
    combine_nutritional_menu,
    search_candidates,
)
from food_agent_v2.contracts.artifacts import (
    FinalValidationArtifact,
    QueryPlanArtifact,
)

logger = logging.getLogger(__name__)


class AgentToolExecutor:
    """Agent 工具统一执行器。复用底层工具算法与回执链，输出脱敏 Observation。"""

    def __init__(self, tool_context: ToolContext, c4: Any, session_id: str) -> None:
        self.tool_context = tool_context
        self.c4 = c4
        self.session_id = session_id
        self.handler = ToolHandler(tool_context)

    def execute_action(
        self,
        action: AgentAction,
        query_plan: QueryPlanArtifact | None = None,
        locked_recipe_ids: list[int] | None = None,
        rejected_recipe_ids: list[int] | None = None,
    ) -> Observation:
        """分发执行工具行动并生成脱敏观察结果。"""
        try:
            if action.action == ActionType.READ_MENU:
                return self._exec_read_menu(action)
            elif action.action in (ActionType.SEARCH_CANDIDATES, ActionType.EXPAND_CANDIDATES):
                return self._exec_search(action)
            elif action.action == ActionType.AUDIT_RECIPE_HEALTH:
                return self._exec_audit(action, locked_recipe_ids)
            elif action.action == ActionType.COMBINE_NUTRITIONAL_MENU:
                return self._exec_combine(action, query_plan, locked_recipe_ids, rejected_recipe_ids)
            elif action.action == ActionType.VALIDATE_SELECTED_MENU:
                return self._exec_validate(action)
            else:
                return Observation(
                    action=action.action,
                    status="error",
                    error_code="UNSUPPORTED_ACTION",
                    message=f"不支持的工具行动: {action.action}",
                )
        except Exception as exc:
            logger.exception("工具执行异常: %s", exc)
            return Observation(
                action=action.action,
                status="error",
                error_code="TOOL_EXECUTION_EXCEPTION",
                message=str(exc),
            )

    def _exec_read_menu(self, action: AgentAction) -> Observation:
        target = action.arguments.get("target", "current")
        state = self.c4.get_session_state(self.session_id) or {} if self.c4 else {}
        if target == "current":
            menu = state.get("current_menu")
        else:
            hist = state.get("menu_history") or []
            menu = hist[-2] if len(hist) >= 2 else None

        if not menu:
            return Observation(
                action=action.action,
                status="no_solution",
                message=f"未找到目标菜单 ({target})",
            )

        recipe_ids = menu.get("recipe_ids", [])
        return Observation(
            action=action.action,
            status="ok",
            desensitized_facts={
                "target": target,
                "plan_id": menu.get("plan_id"),
                "recipe_ids": list(recipe_ids),
                "dish_count": len(recipe_ids),
            },
        )

    def _exec_search(self, action: AgentAction) -> Observation:
        query = action.arguments.get("query", "")
        meal_type = action.arguments.get("meal_type")
        tool_res = search_candidates(query=query, tool_context=self.tool_context, meal_type=meal_type)
        if tool_res.status == "error":
            return Observation(
                action=action.action,
                status="error",
                error_code=tool_res.error_code,
                message=tool_res.message,
            )
        if not tool_res.success:
            return Observation(
                action=action.action,
                status="no_solution",
                error_code=tool_res.error_code,
                desensitized_facts={"total_candidates": 0},
                message=tool_res.message,
            )

        candidates = (tool_res.data or {}).get("candidates", [])
        return Observation(
            action=action.action,
            status="ok",
            desensitized_facts={
                "candidate_count": len(candidates),
                "candidate_recipe_ids": list(candidates),
            },
            evidence_ref=f"retrieval:{len(candidates)}",
        )

    def _exec_audit(
        self, action: AgentAction, locked_recipe_ids: list[int] | None = None
    ) -> Observation:
        candidate_ids = action.arguments.get("candidate_recipe_ids") or []
        if not candidate_ids:
            # 从 tool_context 中获取最近的检索候选
            retrieval_res = self.tool_context.previous_results.get("retrieval_result")
            if retrieval_res and hasattr(retrieval_res, "candidates"):
                candidate_ids = [c.recipe_id for c in retrieval_res.candidates]

        tool_res = audit_recipe_health(
            candidate_recipe_ids=candidate_ids,
            tool_context=self.tool_context,
            locked_recipe_ids=locked_recipe_ids or [],
        )

        if tool_res.status == "error":
            return Observation(
                action=action.action,
                status="error",
                error_code=tool_res.error_code,
                message=tool_res.message,
            )
        if not tool_res.success:
            return Observation(
                action=action.action,
                status="no_solution",
                error_code=tool_res.error_code,
                desensitized_facts={
                    "safe_count": 0,
                    "excluded_count": len(candidate_ids),
                },
                message=tool_res.message,
            )

        safe_ids = (tool_res.data or {}).get("safe_recipe_ids", [])
        excluded_ids = (tool_res.data or {}).get("excluded_recipe_ids", [])
        return Observation(
            action=action.action,
            status="ok",
            desensitized_facts={
                "safe_count": len(safe_ids),
                "excluded_count": len(excluded_ids),
                "safe_recipe_ids": list(safe_ids),
            },
            evidence_ref="health_evaluation:ok",
        )

    def _exec_combine(
        self,
        action: AgentAction,
        query_plan: QueryPlanArtifact | None,
        locked_recipe_ids: list[int] | None = None,
        rejected_recipe_ids: list[int] | None = None,
    ) -> Observation:
        dish_count = action.arguments.get("dish_count")
        if dish_count is None and query_plan:
            dish_count = query_plan.dish_count_requested or 5
        elif dish_count is None:
            dish_count = 5

        # 从上下文获取健康审核后的 safe_recipe_ids
        safe_ids = self.tool_context.previous_results.get("safe_recipe_ids", [])
        if not safe_ids:
            batch = self.tool_context.previous_results.get("health_evaluation_batch")
            if batch and hasattr(batch, "safe_recipe_ids"):
                safe_ids = batch.safe_recipe_ids

        time_limit = (
            query_plan.time_constraint_seconds
            if (query_plan and query_plan.time_constraint_policy == "hard")
            else None
        )

        retrieval_candidates = []
        retrieval_res = self.tool_context.previous_results.get("retrieval_result")
        if retrieval_res and hasattr(retrieval_res, "candidates"):
            retrieval_candidates = retrieval_res.candidates

        res = combine_nutritional_menu(
            safe_recipe_ids=list(safe_ids),
            dish_count=dish_count,
            nutrition_goal_codes=getattr(query_plan, "nutrition_goal_codes", ()),
            time_limit_seconds=time_limit,
            locked_recipe_ids=set(locked_recipe_ids or []),
            rejected_recipe_ids=set(rejected_recipe_ids or []),
            retrieval_candidates=retrieval_candidates,
            dish_types=getattr(query_plan, "dish_types", ()),
            tool_context=self.tool_context,
        )

        if res.status == "error":
            return Observation(
                action=action.action,
                status="error",
                error_code=res.error_code,
                message=res.message,
            )

        if not res.success:
            return Observation(
                action=action.action,
                status="no_solution",
                error_code=res.error_code,
                desensitized_facts={
                    "diagnostics": res.diagnostics or {},
                    "message": res.message,
                },
                message=res.message,
            )

        plans = (res.data or {}).get("plans", [])
        plan_summaries = [
            {
                "plan_id": p.plan_id,
                "recipe_ids": list(p.recipe_ids),
                "estimated_minutes": round(p.estimated_makespan_seconds / 60),
                "total_score": round(p.total_score, 1),
            }
            for p in plans
        ]
        return Observation(
            action=action.action,
            status="ok",
            desensitized_facts={
                "feasible_plan_count": len(plans),
                "plans": plan_summaries,
            },
            evidence_ref=f"feasible_menu:{len(plans)}",
        )

    def _exec_validate(self, action: AgentAction) -> Observation:
        plan_id = action.arguments.get("plan_id")
        recipe_ids = action.arguments.get("recipe_ids", [])
        self.handler.execute(
            "validate_selected_menu_health",
            {"plan_id": plan_id, "recipe_ids": list(recipe_ids)},
        )
        fv = self.tool_context.previous_results.get("final_validation")
        if isinstance(fv, FinalValidationArtifact):
            return Observation(
                action=action.action,
                status="ok" if fv.status == "PASS" else "no_solution",
                desensitized_facts={
                    "plan_id": fv.plan_id,
                    "validation_status": fv.status,
                    "menu_hash": fv.menu_hash,
                },
                evidence_ref=str(fv.artifact_id),
            )
        return Observation(
            action=action.action,
            status="error",
            error_code="VALIDATION_FAILED",
            message="最终健康校验未产生有效产物",
        )
