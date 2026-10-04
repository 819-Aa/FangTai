"""C3 Agent 专用工具集 —— 包装确定性领域能力（C1 检索、B4 健康审查、C2 营养菜单规划）。

契约保证：
1. 权限与边界隔离：工具只做事实计算与审查，不修改共享会话状态；
2. 零静默放宽（Zero Silent Relaxation）：当菜数不足、耗时超标或无解时，绝不自动放宽，
   而是返回精确的归因诊断（diagnostics），交由上层 Agent 追问协商。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from food_agent_v2.b2 import UserHealthProfileService
from food_agent_v2.b3.recipe_views import get_view_builder
from food_agent_v2.b4 import HealthRuleEngine
from food_agent_v2.b4.engine import (
    HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE,
    HEALTH_INGREDIENT_SET_INCOMPLETE,
)
from food_agent_v2.b4.schemas import HealthEvaluationReceipt, HealthIngredientOccurrence
from food_agent_v2.c1 import get_retrieval_service
from food_agent_v2.c1.filters import RetrievalFilters
from food_agent_v2.c2 import MenuHardConstraints, MenuPlanner
from food_agent_v2.c3.fast_intent import retrieval_dish_types

logger = logging.getLogger(__name__)

T = TypeVar("T")

ToolStatus = Literal["ok", "no_solution", "error"]


@dataclass
class ToolResponse(Generic[T]):
    """强类型工具返回封装（遵循 Section 5.2 / Section 12 R2 协议）。"""

    success: bool
    status: ToolStatus = "ok"
    data: T | None = None
    error_code: str | None = None
    message: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status == "ok" and not self.success:
            if self.error_code in (
                "CANDIDATES_REQUIRED",
                "NO_SAFE_CANDIDATE",
                "SAFE_CANDIDATE_SHORTAGE",
                "TIME_LIMIT_EXCEEDED",
                "NO_FEASIBLE_MENU",
            ):
                self.status = "no_solution"
            else:
                self.status = "error"


def _retrieval_filters_from_query_plan(query_plan: Any) -> RetrievalFilters:
    if query_plan is None:
        return RetrievalFilters()
    return RetrievalFilters(
        meal_tags=tuple(getattr(query_plan, "meal_types", ()) or ()),
        population_tags=tuple(getattr(query_plan, "population_tags", ()) or ()),
        dish_type_tags=retrieval_dish_types(query_plan),
        taste_tags=tuple(getattr(query_plan, "taste_tags", ()) or ()),
        cuisine_tags=tuple(getattr(query_plan, "cuisine_tags", ()) or ()),
        scenario_tags=tuple(getattr(query_plan, "scenario_tags", ()) or ()),
        include_ingredients=tuple(
            getattr(query_plan, "include_ingredients", ()) or ()
        ),
        exclude_ingredients=tuple(
            getattr(query_plan, "exclude_ingredients", ()) or ()
        ),
    )


def search_candidates(
    query: str,
    top_k: int = 40,
    query_plan: Any = None,
    participant_user_mapping: dict[str, int] | None = None,
    build_id: str = "",
) -> ToolResponse[dict[str, Any]]:
    """工具 1：C1 混合检索菜品候选。"""
    svc = get_retrieval_service()
    filters = _retrieval_filters_from_query_plan(query_plan)
    projector = getattr(svc, "project_filters", None)
    if callable(projector):
        filters = projector(filters)

    actual_top_k = max(int(top_k), 40)
    mapping = participant_user_mapping or {}

    try:
        if len(mapping) > 1:
            b2 = UserHealthProfileService()
            b2.load(expected_build_id=build_id)
            prefs = []
            for uid in mapping.values():
                u = b2.get_user(uid)
                prefs.append(u.get("dietary_preferences", []) if u else [])
            if any(prefs):
                try:
                    result = svc.multi_person_retrieve(
                        query, prefs, filters=filters, top_k=max(actual_top_k, 30)
                    )
                except Exception:
                    result = svc.retrieve(query, filters=filters, top_k=actual_top_k)
            else:
                result = svc.retrieve(query, filters=filters, top_k=actual_top_k)
        else:
            result = svc.retrieve(query, filters=filters, top_k=actual_top_k)
    except Exception as exc:
        return ToolResponse(
            success=False,
            status="error",
            error_code="RETRIEVAL_FAILED",
            message=f"检索服务调用失败: {exc}",
            diagnostics={"query": query},
        )

    candidates = getattr(result, "candidates", []) or []
    candidate_ids = [c.recipe_id for c in candidates]

    if not candidate_ids:
        return ToolResponse(
            success=False,
            status="no_solution",
            data={"candidates": [], "retrieval_result": result},
            error_code="CANDIDATES_REQUIRED",
            message="未能检索到符合条件的菜品候选",
            diagnostics={"query": query, "total_candidates": 0},
        )

    return ToolResponse(
        success=True,
        status="ok",
        data={
            "candidates": candidate_ids,
            "retrieval_result": result,
            "total": getattr(result, "total_candidates", len(candidate_ids)),
        },
    )


def audit_recipe_health(
    candidate_recipe_ids: list[int],
    participant_user_mapping: dict[str, int],
    build_id: str,
    context_service: Any = None,
    session_id: str = "",
    request_id: str = "",
) -> ToolResponse[dict[str, Any]]:
    """工具 2：B4 健康审查。"""
    if not candidate_recipe_ids:
        return ToolResponse(
            success=False,
            status="error",
            error_code="CANDIDATES_REQUIRED",
            message="缺少待评估的候选菜品",
            diagnostics={"missing_reason": "no_candidates"},
        )

    builder = get_view_builder()
    occurrence_map: dict[int, list[HealthIngredientOccurrence]] = {}
    try:
        for rid in candidate_recipe_ids:
            view = builder.build_health_ingredient_view(rid)
            if view:
                relations = getattr(view, "ingredient_relations", None)
                if relations is None:
                    occurrence_map[rid] = [
                        HealthIngredientOccurrence(
                            ingredient_id=int(ingredient_id),
                            condition_type="required",
                            choice_group_id=None,
                            is_default_choice=True,
                            is_process_material=False,
                        )
                        for ingredient_id in view.ingredient_ids
                    ]
                else:
                    occurrence_map[rid] = [
                        HealthIngredientOccurrence(
                            ingredient_id=int(relation.ingredient_id),
                            condition_type=relation.condition_type,
                            choice_group_id=relation.choice_group_id,
                            is_default_choice=bool(relation.is_default_choice),
                            is_process_material=bool(relation.is_process_material),
                        )
                        for relation in relations
                    ]
    except Exception as exc:
        return ToolResponse(
            success=False,
            status="error",
            error_code="HEALTH_AUDIT_ERROR",
            message=f"菜品食材关系构建异常: {exc}",
        )

    try:
        engine = HealthRuleEngine()
        engine.load_relations()
    except (HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE, HEALTH_INGREDIENT_SET_INCOMPLETE) as exc:
        return ToolResponse(
            success=False,
            status="error",
            error_code="HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE",
            message=f"健康规则覆盖不完整: {exc}",
        )
    except Exception as exc:
        return ToolResponse(
            success=False,
            status="error",
            error_code="HEALTH_AUDIT_ERROR",
            message=f"健康规则引擎加载异常: {exc}",
        )

    try:
        b2 = UserHealthProfileService()
        b2.load(expected_build_id=build_id)
    except Exception as exc:
        return ToolResponse(
            success=False,
            status="error",
            error_code="PERMANENT_CONSTRAINT_LOAD_FAILED",
            message=f"用户健康档案加载异常: {exc}",
        )

    all_constraints = {}
    for ref, uid in participant_user_mapping.items():
        try:
            cs = b2.derive_constraints(uid, ref)
            hard = list(cs.hard_constraints)
        except Exception as exc:
            return ToolResponse(
                success=False,
                status="error",
                error_code="PERMANENT_CONSTRAINT_LOAD_FAILED",
                message=f"用户约束派生异常: {exc}",
            )
        to_b4 = (
            getattr(context_service, "to_b4_constraints", None)
            if context_service is not None
            else None
        )
        if to_b4 is not None and session_id:
            try:
                temp = to_b4(session_id)
                hard.extend(c for c in temp if c.participant_ref == ref)
            except Exception as e:
                return ToolResponse(
                    success=False,
                    status="error",
                    error_code="TEMPORARY_CONSTRAINT_LOAD_FAILED",
                    message=f"临时约束加载失败: {e}",
                )
        all_constraints[ref] = hard

    try:
        batch: HealthEvaluationReceipt = engine.evaluate_batch_occurrences(
            candidate_recipe_ids, occurrence_map, all_constraints
        )
        if request_id:
            batch.request_id = request_id
    except (HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE, HEALTH_INGREDIENT_SET_INCOMPLETE) as exc:
        return ToolResponse(
            success=False,
            status="error",
            error_code="HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE",
            message=f"健康约束覆盖不完整: {exc}",
        )
    except Exception as exc:
        return ToolResponse(
            success=False,
            status="error",
            error_code="HEALTH_AUDIT_ERROR",
            message=f"健康审核执行异常: {exc}",
        )

    safe_ids = list(getattr(batch, "safe_recipe_ids", []) or [])
    excluded_ids = list(getattr(batch, "excluded_recipe_ids", []) or [])

    # 提取排除原因详情（精确到菜品与禁忌/疾病原因）
    excluded_details: dict[int, list[str]] = {}
    for r in getattr(batch, "participant_recipe_results", []):
        if r.verdict == "EXCLUDE":
            for hc in getattr(r, "hitting_constraints", []):
                code = hc.get("constraint_code") or hc.get("reason") or str(hc)
                excluded_details.setdefault(r.recipe_id, []).append(
                    f"{r.participant_ref}: {code}"
                )

    if not safe_ids:
        return ToolResponse(
            success=False,
            status="no_solution",
            data={
                "safe_recipe_ids": [],
                "excluded_recipe_ids": excluded_ids,
                "batch": batch,
                "excluded_details": excluded_details,
            },
            error_code="NO_SAFE_CANDIDATE",
            message="健康审查完成（所有候选菜品均不符合健康安全要求）",
            diagnostics={
                "total_evaluated": len(candidate_recipe_ids),
                "excluded_details": excluded_details,
                "cause": "ALL_CANDIDATES_EXCLUDED_BY_HEALTH",
            },
        )

    return ToolResponse(
        success=True,
        status="ok",
        data={
            "safe_recipe_ids": safe_ids,
            "excluded_recipe_ids": excluded_ids,
            "batch": batch,
            "excluded_details": excluded_details,
        },
    )


def combine_nutritional_menu(
    safe_recipe_ids: list[int],
    dish_count: int | None = None,
    nutrition_goal_codes: tuple[str, ...] = (),
    time_limit_seconds: int | None = None,
    locked_recipe_ids: set[int] | None = None,
    rejected_recipe_ids: set[int] | None = None,
    retrieval_candidates: list[Any] | None = None,
    dish_types: tuple[str, ...] = (),
    tool_context: Any | None = None,
) -> ToolResponse[dict[str, Any]]:
    """工具 3：C2 营养菜单组合与瓶颈诊断（零静默放宽）。

    契约保证：
    1. 默认菜数复用 MenuHardConstraints 默认值（5 菜），绝不在适配层硬编码 4；
    2. 当提供 tool_context 时，通过 ToolHandler 执行唯一一次规划并记录权威回执；
    3. 成功路径绝不重复规划；仅在无可行方案时运行隔离的瓶颈诊断。
    """
    actual_dish_count = (
        dish_count
        if dish_count is not None
        else MenuHardConstraints().dish_count
    )

    if not safe_recipe_ids:
        return ToolResponse(
            success=False,
            status="no_solution",
            error_code="NO_SAFE_CANDIDATE",
            message="没有可用的安全候选菜品进行组合",
            diagnostics={"safe_count": 0, "requested_count": actual_dish_count},
        )

    locked = locked_recipe_ids or set()
    rejected = rejected_recipe_ids or set()
    available = [r for r in safe_recipe_ids if r not in rejected]

    dish_type_set = {str(item).strip().lower() for item in dish_types}

    def _requests(*labels: str) -> bool:
        return any(label.lower() in dish_type_set for label in labels)

    # 1. 如果提供了 tool_context，统一通过 ToolHandler 执行唯一规划（生成权威回执与上下文）
    if tool_context is not None:
        from food_agent_v2.c3.tool_handler import ToolHandler

        handler = ToolHandler(tool_context)
        res = handler.execute(
            "generate_feasible_menus",
            {
                "safe_recipe_ids": safe_recipe_ids,
                "dish_count": actual_dish_count,
                "locked_recipe_ids": list(locked),
                "rejected_recipe_ids": list(rejected),
            },
        )
        if isinstance(res, dict) and "error" in res:
            return ToolResponse(
                success=False,
                status="error",
                error_code=res.get("error"),
                message=res.get("note") or res.get("error"),
            )
        plans = tool_context.previous_results.get("feasible_menus", [])
        if plans:
            return ToolResponse(
                success=True,
                status="ok",
                data={"plans": plans, "count": len(plans)},
            )
    else:
        # 独立运行模式（用于不带 tool_context 的单步测试或直接调用）
        if len(available) >= actual_dish_count:
            planner = MenuPlanner()
            planner.set_safe_candidates(safe_recipe_ids)
            try:
                builder = get_view_builder()
                features = {}
                ranked = list(retrieval_candidates or [])
                rank_scores = {
                    int(c.recipe_id): (len(ranked) - index) / max(len(ranked), 1)
                    for index, c in enumerate(ranked)
                }
                for rid in safe_recipe_ids:
                    rv = builder.build_retrieval_view(rid)
                    if rv:
                        features[rid] = {
                            "name": rv.name,
                            "fields": rv.searchable_fields,
                            "preference_score": rank_scores.get(rid, 0.0),
                        }
                planner.set_recipe_features(features)
            except Exception:
                pass

            hard = MenuHardConstraints(
                dish_count=actual_dish_count,
                max_estimated_time_seconds=time_limit_seconds,
                locked_recipe_ids=locked,
                rejected_recipe_ids=rejected,
                require_soup=_requests("汤", "汤品", "soup"),
                require_staple=_requests("主食", "staple"),
                require_drink=_requests("饮品", "饮料", "drink"),
                require_dessert=_requests("甜品", "甜点", "dessert"),
            )
            plans = planner.plan(
                hard,
                target_count=5,
                nutrition_goal_codes=nutrition_goal_codes,
            )
            if plans:
                return ToolResponse(
                    success=True,
                    status="ok",
                    data={"plans": plans, "count": len(plans)},
                )

    # 2. 规划无可行方案时的隔离瓶颈诊断（与正式方案和权威回执严格隔离）
    # 诊断分支 A：安全候选菜品不足
    if len(available) < actual_dish_count:
        return ToolResponse(
            success=False,
            status="no_solution",
            error_code="SAFE_CANDIDATE_SHORTAGE",
            message=f"安全候选菜品不足（当前仅 {len(available)} 道安全菜，要求 {actual_dish_count} 道）",
            diagnostics={
                "bottleneck": "SAFE_CANDIDATE_SHORTAGE",
                "safe_count": len(available),
                "requested_count": actual_dish_count,
                "deficit": actual_dish_count - len(available),
            },
        )

    # 诊断分支 B：如果是受时间硬上限限制导致的失败？
    if time_limit_seconds is not None:
        diag_planner = MenuPlanner()
        diag_planner.set_safe_candidates(safe_recipe_ids)
        try:
            builder = get_view_builder()
            features = {}
            ranked = list(retrieval_candidates or [])
            rank_scores = {
                int(c.recipe_id): (len(ranked) - index) / max(len(ranked), 1)
                for index, c in enumerate(ranked)
            }
            for rid in safe_recipe_ids:
                rv = builder.build_retrieval_view(rid)
                if rv:
                    features[rid] = {
                        "name": rv.name,
                        "fields": rv.searchable_fields,
                        "preference_score": rank_scores.get(rid, 0.0),
                    }
            diag_planner.set_recipe_features(features)
        except Exception:
            pass

        unconstrained_hard = MenuHardConstraints(
            dish_count=actual_dish_count,
            max_estimated_time_seconds=None,
            locked_recipe_ids=locked,
            rejected_recipe_ids=rejected,
            require_soup=_requests("汤", "汤品", "soup"),
            require_staple=_requests("主食", "staple"),
            require_drink=_requests("饮品", "饮料", "drink"),
            require_dessert=_requests("甜品", "甜点", "dessert"),
        )
        fallback_plans = diag_planner.plan(
            unconstrained_hard,
            target_count=5,
            nutrition_goal_codes=nutrition_goal_codes,
        )
        if fallback_plans:
            min_makespan = min(p.estimated_makespan_seconds for p in fallback_plans)
            min_needed_minutes = max(1, round(min_makespan / 60))
            requested_minutes = max(1, round(time_limit_seconds / 60))
            return ToolResponse(
                success=False,
                status="no_solution",
                error_code="TIME_LIMIT_EXCEEDED",
                message=(
                    f"制作时间限制超限（要求 {requested_minutes} 分钟内完成，"
                    f"基于当前安全候选菜谱，最快制作组合预计需要约 {min_needed_minutes} 分钟）"
                ),
                diagnostics={
                    "bottleneck": "TIME_LIMIT_EXCEEDED",
                    "requested_time_minutes": requested_minutes,
                    "min_needed_minutes": min_needed_minutes,
                    "requested_dish_count": actual_dish_count,
                    "safe_count": len(available),
                },
            )

    return ToolResponse(
        success=False,
        status="no_solution",
        error_code="NO_FEASIBLE_MENU",
        message="现有安全菜品无法组合出满足所有结构与品类要求的菜单方案",
        diagnostics={
            "bottleneck": "STRUCTURAL_OR_COMBINATORIAL_UNSATISFIABLE",
            "safe_count": len(available),
            "requested_dish_count": actual_dish_count,
        },
    )
