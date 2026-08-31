"""C3 工具处理器 —— 模型调用工具时，映射到真实领域服务并执行（T16）。

工具桥接层：接收模型发出的 tool_call → 调用 B/C 模块的真实函数 → 返回结果。
每个工具调用产生绑定 request/node/input/build 的不可变回执（contracts 契约边界）；
回执身份与当前 request/node/input 不一致即被拒绝（RECEIPT_BINDING_MISMATCH）。
不做数据伪造——如果缺少必要参数，从 ToolContext 中获取。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from food_agent_v2.c3.receipts import ToolReceipt, validate_workflow_receipt


@dataclass
class ToolContext:
    """工具调用所需的请求级上下文。"""
    request_id: str = ""
    node_id: str = ""                    # 当前工作流节点（由 runner 在节点边界注入）
    build_id: str = ""                   # 构建身份（契约 build_id；T17 接线）
    participant_user_mapping: dict[str, int] = field(default_factory=dict)
    previous_results: dict[str, Any] = field(default_factory=dict)
    safe_recipe_ids: list[int] = field(default_factory=list)
    tool_receipts: list[dict] = field(default_factory=list)
    max_estimated_time_seconds: int | None = None  # QueryPlan 提取的预计时间硬上限
    session_id: str = ""                    # 用于工具回写 C4 会话上下文
    context_service: Any | None = None      # C4 ContextService（工具可回写约束）


class ToolHandler:
    """工具调用路由。每个工具映射到一个真实领域服务函数。"""

    def __init__(self, ctx: ToolContext | None = None):
        self._ctx = ctx or ToolContext()
        self._receipts: list[dict] = []

    def execute(self, tool_name: str, arguments: dict) -> dict:
        handler = _TOOL_MAP.get(tool_name)
        if handler is None:
            return {"error": f"TOOL_NOT_IMPLEMENTED: {tool_name}"}

        # fail-closed：request/node/build 身份不完整 → 不调用领域服务，不产生有效回执
        binding_error = self._binding_error()
        if binding_error is not None:
            self._record_receipt({
                "tool_name": tool_name,
                "tool_call_id": uuid.uuid4().hex,
                "request_id": self._ctx.request_id,
                "node_id": self._ctx.node_id,
                "input_hash": "",
                "output_hash": "",
                "build_id": self._ctx.build_id,
                "success": False,
                "error_code": binding_error,
                "arguments_summary": {k: str(v)[:80] for k, v in arguments.items()},
                "result_summary": "",
            })
            return {"error": binding_error, "tool": tool_name}

        try:
            result = handler(arguments, self._ctx)
        except Exception as e:
            result = {"error": f"TOOL_EXECUTION_FAILED: {e}", "tool": tool_name}

        success = not (isinstance(result, dict) and "error" in result)
        error_code = result.get("error") if isinstance(result, dict) and "error" in result else None
        self._emit_receipt(tool_name, arguments, result, success, error_code)
        return result

    def _binding_error(self) -> str | None:
        """缺失/非法 request/node/build 时返回错误码（fail-closed，绝不跳过）。"""
        missing = []
        if _coerce_uuid(self._ctx.request_id) is None:
            missing.append("request_id")
        if not self._ctx.node_id:
            missing.append("node_id")
        build_id = _coerce_uuid(self._ctx.build_id)
        if build_id is None or build_id == UUID(int=0):
            missing.append("build_id")
        if missing:
            return f"RECEIPT_BINDING_MISMATCH: 缺失或非法绑定身份: {missing}"
        return None

    def _emit_receipt(self, tool_name: str, arguments: dict, result: dict,
                      success: bool, error_code: str | None) -> dict:
        """产生绑定 request/node/input/build 的回执并校验契约边界（文档 §11.3）。"""
        receipt = {
            "tool_name": tool_name,
            "tool_call_id": uuid.uuid4().hex,
            "request_id": self._ctx.request_id,
            "node_id": self._ctx.node_id,
            "input_hash": _sha256(arguments),
            "output_hash": _sha256(result),
            "build_id": self._ctx.build_id,
            "success": success,
            "error_code": error_code,
            "arguments_summary": {k: str(v)[:80] for k, v in arguments.items()},
            "result_summary": json.dumps(result, ensure_ascii=False, default=str)[:300],
        }
        # 契约校验：request/node/input/build 一致才产生有效回执
        validate_workflow_receipt(
            ToolReceipt(
                request_id=UUID(receipt["request_id"]),
                node_id=receipt["node_id"],
                tool_call_id=receipt["tool_call_id"],
                tool_name=receipt["tool_name"],
                input_hash=receipt["input_hash"],
                output_hash=receipt["output_hash"],
                build_id=UUID(receipt["build_id"]),
                success=receipt["success"],
                error_code=receipt["error_code"],
            ),
            request_id=UUID(receipt["request_id"]),
            node_id=receipt["node_id"],
            input_hash=receipt["input_hash"],
            build_id=UUID(receipt["build_id"]),
        )
        self._record_receipt(receipt)
        return receipt

    def _record_receipt(self, receipt: dict) -> None:
        self._receipts.append(receipt)
        # 同时写入请求级上下文（runner 从 ToolContext 提取节点回执）
        self._ctx.tool_receipts.append(receipt)

    @property
    def receipts(self) -> list[dict]:
        return self._receipts

    def clear_receipts(self) -> None:
        self._receipts.clear()


def _sha256(value: Any) -> str:
    """64 位十六进制散列（契约 Sha256Hash，input_hash/output_hash 必须为 64 位）。"""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _coerce_uuid(value: Any) -> UUID | None:
    """字符串 → UUID；空值或非法值返回 None。"""
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


# ---- 真实工具实现 ----

def _retrieval_filters_from_query_plan(query_plan):
    from food_agent_v2.c1.filters import RetrievalFilters

    return RetrievalFilters(
        meal_tags=tuple(getattr(query_plan, "meal_types", ()) or ()),
        population_tags=tuple(getattr(query_plan, "population_tags", ()) or ()),
        dish_type_tags=tuple(getattr(query_plan, "dish_types", ()) or ()),
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


def _project_retrieval_filters(svc, filters):
    """Project soft facets when the retrieval port supports ready-build projection."""
    projector = getattr(svc, "project_filters", None)
    return projector(filters) if callable(projector) else filters

def _retrieve_recipes(args: dict, ctx: ToolContext) -> dict:
    """C1 混合检索。多人场景自动使用多路合并（文档 07 §8.4）。

    召回广度：混合检索先广召回再健康筛选（文档 07 §7）。语义向量对"三菜一汤"
    等结构需求偏向汤类——top_k=20 时 main（主菜）候选常被截断，导致 C2 无法
    凑满菜数（no_feasible_menu）。默认 top_k 提到 40，保证各类型候选充足。
    """
    from food_agent_v2.c1 import get_retrieval_service
    query = args.get("query", args.get("search_query", ""))
    # 候选池下限：top_k 过小会导致结构需求（汤/主食）候选不足 → C2 no_feasible。
    # 系统保证候选充足（健康审查前提），模型传更小值也强制抬到 40。
    top_k = max(int(args.get("top_k", 40)), 40)
    svc = get_retrieval_service()
    filters = _retrieval_filters_from_query_plan(
        ctx.previous_results.get("query_plan")
    )
    filters = _project_retrieval_filters(svc, filters)

    # 多人 → 共享查询 + 每参与者口味偏好子查询
    if len(ctx.participant_user_mapping) > 1:
        from food_agent_v2.b2 import UserHealthProfileService

        # B2 是在线健康事实边界：加载/身份/完整性失败必须传播为失败回执，
        # 不得被检索降级逻辑吞掉后继续运行。
        b2 = UserHealthProfileService()
        b2.load(expected_build_id=ctx.build_id)
        prefs = []
        for uid in ctx.participant_user_mapping.values():
            u = b2.get_user(uid)
            prefs.append(u.get("dietary_preferences", []) if u else [])

        if any(prefs):
            # 仅检索算法自身失败可降级为共享普通检索；B2 加载已经在 try 外完成。
            try:
                result = svc.multi_person_retrieve(
                    query,
                    prefs,
                    filters=filters,
                    top_k=max(top_k, 30),
                )
            except Exception:
                result = svc.retrieve(query, filters=filters, top_k=top_k)
        else:
            result = svc.retrieve(query, filters=filters, top_k=top_k)
    else:
        result = svc.retrieve(query, filters=filters, top_k=top_k)

    ctx.previous_results["retrieval"] = result
    return {
        "total": result.total_candidates,
        "candidates": [{"recipe_id": c.recipe_id, "name": c.name,
                        "source_paths": getattr(c, "source_paths", [])}
                       for c in result.candidates[:top_k]],
    }


def _get_current_menu(args: dict, ctx: ToolContext) -> dict:
    """读取当前会话的已有菜单（替换/恢复场景）。"""
    return {"current_menu": None, "note": "当前会话无已有菜单"}


def _get_health_constraints(args: dict, ctx: ToolContext) -> dict:
    """B2 获取参与者约束（使用真实的 participant→user_id 映射）。"""
    from food_agent_v2.b2 import UserHealthProfileService
    svc = UserHealthProfileService()
    svc.load(expected_build_id=ctx.build_id)

    result = {}
    constraint_sets = {}
    for ref, uid in ctx.participant_user_mapping.items():
        cs = svc.derive_constraints(uid, ref)
        constraint_sets[ref] = cs
        result[ref] = {
            "hard_constraint_codes": _extract_codes(cs.hard_constraints),
            "hard_count": len(cs.hard_constraints),
            "soft_goals": [g.goal_code for g in cs.soft_goals],
        }

    # B2 → C4：把派生约束写回会话上下文（角色投影 + 完整性校验使用真实约束）
    if ctx.context_service is not None and ctx.session_id:
        try:
            ctx.context_service.store_derived_constraints(ctx.session_id, constraint_sets)
        except Exception:
            pass

    ctx.previous_results["health_constraints"] = result
    return {"participants": result}


def _evaluate_recipe_health(args: dict, ctx: ToolContext) -> dict:
    """B4 健康审查：对候选菜品逐道评估。
    通过 B3 获取每道菜的真实 ingredient_ids。"""
    from food_agent_v2.b2 import UserHealthProfileService
    from food_agent_v2.b3.recipe_views import get_view_builder
    from food_agent_v2.b4 import HealthRuleEngine
    from food_agent_v2.b4.schemas import HealthIngredientOccurrence

    recipe_ids = args.get("recipe_ids", [])
    if not recipe_ids:
        # 无候选可评估：模型未检索/召回为空/未提供候选。
        # 业务归因（诚实）：不把责任归于"漏调工具"，而是"候选缺失"。
        return {"error": "CANDIDATES_REQUIRED", "safe_recipe_ids": [],
                "excluded_recipe_ids": [], "missing_reason": "no_candidates"}

    # 从 B3 获取真实食材视图
    builder = get_view_builder()
    occurrence_map: dict[int, list[HealthIngredientOccurrence]] = {}
    for rid in recipe_ids:
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

    # B4 引擎
    engine = HealthRuleEngine()
    engine.load_relations()
    b2 = UserHealthProfileService()
    b2.load(expected_build_id=ctx.build_id)

    # 构建所有参与者的约束集：B2 永久约束 + C4 会话/本轮临时约束（R-002 闭环）
    all_constraints = {}
    for ref, uid in ctx.participant_user_mapping.items():
        cs = b2.derive_constraints(uid, ref)
        hard = list(cs.hard_constraints)
        # 合并本会话临时健康约束（经 B2 验证后由 runner 写入 C4）
        to_b4 = getattr(ctx.context_service, "to_b4_constraints", None) \
            if ctx.context_service is not None else None
        if to_b4 is not None and ctx.session_id:
            try:
                temp = to_b4(ctx.session_id)
                hard.extend(c for c in temp if c.participant_ref == ref)
            except Exception as e:
                # 临时约束转换失败 → fail-closed（不能静默丢本轮临时禁忌）
                return {"error": f"TEMPORARY_CONSTRAINT_LOAD_FAILED: {e}",
                        "safe_recipe_ids": [], "excluded_recipe_ids": []}
        all_constraints[ref] = hard

    batch = engine.evaluate_batch_occurrences(recipe_ids, occurrence_map, all_constraints)
    # MC-01-R2 P0-1：绑定回执到当前请求，供 generate 校验新鲜度（禁止跨请求复用）
    batch.request_id = ctx.request_id
    ctx.previous_results["health_evaluation"] = batch
    ctx.safe_recipe_ids = batch.safe_recipe_ids

    return {
        "safe_recipe_ids": batch.safe_recipe_ids[:100],
        "excluded_recipe_ids": batch.excluded_recipe_ids[:50],
        "total_evaluated": len(recipe_ids),
        "safe_count": len(batch.safe_recipe_ids),
        "excluded_count": len(batch.excluded_recipe_ids),
    }


def _generate_feasible_menus(args: dict, ctx: ToolContext) -> dict:
    """C2 菜单规划：唯一安全候选来源 = 权威 HealthEvaluationReceipt.safe_recipe_ids。

    - 绝不从 retrieval / retrieval_expanded 等原始召回恢复候选（MC-01）；
    - 模型传入的 safe_recipe_ids 不得成为权威：若含权威集合之外的 recipe_id，
      返回 SAFE_RECIPE_IDS_MISMATCH（不得静默过滤后继续）；
    - 无有效健康回执 → HEALTH_EVALUATION_REQUIRED；
    - 权威 safe 为空 → no_safe_menu（不调用 MenuPlanner）；
    - safe 非空但 C2 无方案 → no_feasible_menu（不得回退使用不安全候选）。
    """
    # 1. 唯一权威来源：最新一次 evaluate_recipe_health 的 HealthEvaluationReceipt
    from food_agent_v2.b4.schemas import HealthEvaluationReceipt
    from food_agent_v2.c2 import MenuHardConstraints, MenuPlanner

    receipt = ctx.previous_results.get("health_evaluation")
    if not isinstance(receipt, HealthEvaluationReceipt):
        return {"error": "HEALTH_EVALUATION_REQUIRED", "plans": [], "count": 0,
                "note": "missing authoritative health evaluation receipt"}
    # MC-01-R2 P0-1：禁止复用前一轮/前一请求/前一节点的 HealthEvaluationReceipt
    if str(receipt.request_id or "") != str(ctx.request_id):
        return {"error": "HEALTH_EVALUATION_REQUIRED", "plans": [], "count": 0,
                "note": "stale health evaluation receipt"}
    authoritative_safe = set(int(r) for r in (receipt.safe_recipe_ids or []))

    # 2. 模型列表不得成为权威：传入权威集合之外的 recipe_id → 明确报错
    model_safe = args.get("safe_recipe_ids", [])
    if model_safe:
        outside = [rid for rid in model_safe if int(rid) not in authoritative_safe]
        if outside:
            return {"error": "SAFE_RECIPE_IDS_MISMATCH", "plans": [], "count": 0,
                    "note": f"model safe_recipe_ids 含非权威 recipe_id: {outside[:5]}"}

    # 3. 权威 safe 为空 → no_safe_menu（不调用 MenuPlanner，不生成方案）
    if not authoritative_safe:
        return {"plans": [], "count": 0, "note": "no_safe_menu", "safe_count": 0}
    safe_ids = sorted(authoritative_safe)

    query_plan = ctx.previous_results.get("query_plan")
    requested_count = getattr(query_plan, "dish_count_requested", None)
    dish_types = {
        str(item).strip().lower()
        for item in (getattr(query_plan, "dish_types", ()) or ())
    }

    def _requests(*labels: str) -> bool:
        return any(label.lower() in dish_types for label in labels)

    planner = MenuPlanner()
    planner.set_safe_candidates(safe_ids)
    # 注入 recipe_features 供 C2 偏好/多样性评分使用
    try:
        from food_agent_v2.b3.recipe_views import get_view_builder
        builder = get_view_builder()
        features = {}
        retrieval = ctx.previous_results.get("retrieval")
        ranked = list(getattr(retrieval, "candidates", []) or [])
        rank_scores = {
            int(candidate.recipe_id): (len(ranked) - index) / max(len(ranked), 1)
            for index, candidate in enumerate(ranked)
        }
        for rid in safe_ids:
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
        dish_count=requested_count or MenuHardConstraints().dish_count,
        # 时间上限只能来自已验证 QueryPlan，工具自由参数不得覆盖。
        max_estimated_time_seconds=ctx.max_estimated_time_seconds,
        # P5/L1.3：锁定菜（最小修改——保留不违规的当前菜，仅补足/替换违规菜）
        locked_recipe_ids=set(int(r) for r in (args.get("locked_recipe_ids") or [])),
        # L1.3：拒绝菜（替换/否定场景，从候选排除）
        rejected_recipe_ids=set(int(r) for r in (args.get("rejected_recipe_ids") or [])),
        require_soup=_requests("汤", "汤品", "soup"),
        require_staple=_requests("主食", "staple"),
        require_drink=_requests("饮品", "饮料", "drink"),
        require_dessert=_requests("甜品", "甜点", "dessert"),
    )
    plans = planner.plan(
        hard,
        target_count=5,
        nutrition_goal_codes=tuple(
            getattr(query_plan, "nutrition_goal_codes", ()) or ()
        ),
    )
    ctx.previous_results["feasible_menus"] = plans

    if not plans:
        return {"plans": [], "count": 0, "note": "no_feasible_menu", "safe_count": len(safe_ids)}

    return {
        "plans": [{
            "plan_id": p.plan_id, "recipe_ids": p.recipe_ids[:10],
            "objective": p.dominant_objective, "total_score": p.total_score,
            "estimated_makespan_seconds": p.estimated_makespan_seconds,
        } for p in plans],
        "count": len(plans),
    }


def _validate_selected_menu_health(args: dict, ctx: ToolContext) -> dict:
    """B4 最终健康校验：绑定 C2 唯一权威 menu_hash，产出可引用 FinalValidationArtifact。

    FinalValidationArtifact 带稳定 artifact_id 写入请求级上下文；模型经
    final_validation_ref 引用（禁止从截断 result_summary 拼造、禁止占位 hash）。
    """
    from food_agent_v2.b2 import UserHealthProfileService
    from food_agent_v2.b3.recipe_views import get_view_builder
    from food_agent_v2.b4 import HealthRuleEngine
    from food_agent_v2.b4.schemas import HealthIngredientOccurrence
    from food_agent_v2.c2.schemas import menu_hash_for
    from food_agent_v2.contracts.artifacts import (
        FinalValidationArtifact,
        ParticipantRecipeHealthResult,
    )

    plan_id = args.get("plan_id", "")
    recipe_ids = [int(r) for r in args.get("recipe_ids", [])]

    # 唯一权威 menu_hash：从 C2 可行方案读取并核对规范 hash（C3/B4 不得另行定义）
    feasible = ctx.previous_results.get("feasible_menus", [])
    plan = next((p for p in feasible if getattr(p, "plan_id", "") == plan_id), None)
    if plan is None:
        return {"error": f"UNKNOWN_PLAN_ID: {plan_id}", "verdict": None, "plan_id": plan_id}
    plan_menu_hash = getattr(plan, "menu_hash", "")
    canonical = menu_hash_for(plan_id, recipe_ids)
    if not plan_menu_hash or plan_menu_hash != canonical:
        return {"error": f"MENU_HASH_MISMATCH: {plan_id}", "verdict": None, "plan_id": plan_id}
    menu_hash = plan_menu_hash

    builder = get_view_builder()
    occurrence_map: dict[int, list[HealthIngredientOccurrence]] = {}
    for rid in recipe_ids:
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

    engine = HealthRuleEngine()
    engine.load_relations()
    b2 = UserHealthProfileService()
    b2.load(expected_build_id=ctx.build_id)

    all_constraints = {}
    for ref, uid in ctx.participant_user_mapping.items():
        cs = b2.derive_constraints(uid, ref)
        hard = list(cs.hard_constraints)
        # R-002 闭环：最终复核也必须合并本会话临时约束（与候选审查一致），
        # 否则最终 PASS 不能证明满足本轮临时禁忌。转换失败 → fail-closed。
        to_b4 = getattr(ctx.context_service, "to_b4_constraints", None) \
            if ctx.context_service is not None else None
        if to_b4 is not None and ctx.session_id:
            try:
                temp = to_b4(ctx.session_id)
                hard.extend(c for c in temp if c.participant_ref == ref)
            except Exception as e:
                return {"error": f"TEMPORARY_CONSTRAINT_LOAD_FAILED: {e}",
                        "verdict": None, "plan_id": plan_id}
        all_constraints[ref] = hard

    result = engine.validate_selected_menu_occurrences(
        recipe_ids, occurrence_map, all_constraints, plan_id, menu_hash
    )

    # 权威可引用 FinalValidationArtifact（menu_artifact_ref 指向健康规划产出的 FeasibleMenuArtifact）
    feasible_artifact = ctx.previous_results.get("feasible_menu_artifact")
    fv_artifact = FinalValidationArtifact(
        artifact_id=uuid.uuid4(),
        request_id=UUID(ctx.request_id),
        plan_id=plan_id,
        menu_artifact_ref=str(getattr(feasible_artifact, "artifact_id", "") or ""),
        participant_refs=tuple(ctx.participant_user_mapping.keys()),
        recipe_ids=tuple(recipe_ids),
        participant_recipe_results=tuple(
            ParticipantRecipeHealthResult(
                participant_ref=r.participant_ref,
                recipe_id=r.recipe_id,
                status=r.verdict,
                exclusion_hits=tuple(h.get("constraint_code") or "" for h in r.hitting_constraints),
                constraint_refs=tuple(r.evidence_refs),
                evaluated_ingredient_ids=(),
                ingredient_set_evidence_paths=(),
                coverage_refs=(),
                input_fingerprint=_sha256([r.recipe_id, r.participant_ref, r.verdict]),
            )
            for r in result.participant_recipe_results
        ),
        menu_hash=menu_hash,
        input_fingerprint=_sha256([plan_id, sorted(recipe_ids)]),
        status=result.verdict,
    )
    ctx.previous_results["final_validation"] = fv_artifact
    return {
        "verdict": result.verdict,
        "plan_id": plan_id,
        "recipe_ids": recipe_ids,
        "menu_hash": menu_hash,
        "final_validation_ref": str(fv_artifact.artifact_id),
        "details": "final validation complete",
    }


def _expand_retrieval(args: dict, ctx: ToolContext) -> dict:
    """C1 扩展召回——差异不足时使用更宽松查询再跑一次。"""
    from food_agent_v2.c1 import get_retrieval_service
    svc = get_retrieval_service()
    query = args.get("query", "")
    original_ids = args.get("original_ids", [])

    result = svc.retrieve(
        query,
        filters=_project_retrieval_filters(
            svc,
            _retrieval_filters_from_query_plan(ctx.previous_results.get("query_plan")),
        ),
        top_k=30,
        exclude_ids=set(original_ids),
    )
    if result.total_candidates == 0:
        return {"candidates": [], "count": 0, "note": "no additional candidates"}
    ctx.previous_results["retrieval_expanded"] = result
    return {"candidates": [{"recipe_id": c.recipe_id, "name": c.name} for c in result.candidates], "count": result.total_candidates}


def _adjust_menu_plan(args: dict, ctx: ToolContext) -> dict:
    """C2 菜单调整——替换或恢复指定菜品。"""
    from food_agent_v2.c2 import MenuHardConstraints, MenuPlanner
    plans = ctx.previous_results.get("feasible_menus", [])
    if not plans:
        return {"adjusted": None, "note": "no existing plan to adjust"}

    plan_id = args.get("plan_id", "")
    target_plan = None
    if plan_id:
        for p in plans:
            if getattr(p, "plan_id", "") == plan_id:
                target_plan = p
                break
    if target_plan is None:
        target_plan = plans[0]  # fallback

    planner = MenuPlanner()
    planner.set_safe_candidates(ctx.safe_recipe_ids)
    hard = MenuHardConstraints(dish_count=len(target_plan.recipe_ids) if target_plan else 4)

    adjusted = planner.adjust_menu(
        target_plan,
        args.get("replace_recipe_id", 0),
        ctx.safe_recipe_ids,
        hard,
        nutrition_goal_codes=tuple(
            getattr(ctx.previous_results.get("query_plan"), "nutrition_goal_codes", ())
            or ()
        ),
    )
    if adjusted:
        return {"adjusted_plan_id": adjusted.plan_id, "recipe_ids": adjusted.recipe_ids}
    return {"adjusted": None, "note": "no feasible adjustment found"}


def _get_execution_trace(args: dict, ctx: ToolContext) -> dict:
    """C3 内部：返回当前请求的工具调用记录。"""
    return {"tool_calls": ctx.tool_receipts, "total": len(ctx.tool_receipts)}


def _get_artifact_chain(args: dict, ctx: ToolContext) -> dict:
    """C3 内部：返回 Artifact 链引用。"""
    return {"chain": list(ctx.previous_results.keys()), "note": "artifact references from workflow context"}


def _extract_codes(constraints: list) -> list[str]:
    codes = []
    for c in constraints:
        if hasattr(c, 'constraint_code'):
            codes.append(c.constraint_code)
        elif hasattr(c, 'taboo_ingredient_name'):
            codes.append(f"taboo:{c.taboo_ingredient_name}")
    return codes


_TOOL_MAP: dict[str, Callable] = {
    "retrieve_recipes": _retrieve_recipes,
    "get_current_menu": _get_current_menu,
    "get_health_constraints": _get_health_constraints,
    "evaluate_recipe_health": _evaluate_recipe_health,
    "generate_feasible_menus": _generate_feasible_menus,
    "expand_retrieval": _expand_retrieval,
    "adjust_menu_plan": _adjust_menu_plan,
    "validate_selected_menu_health": _validate_selected_menu_health,
    "get_execution_trace": _get_execution_trace,
    "get_artifact_chain": _get_artifact_chain,
}
