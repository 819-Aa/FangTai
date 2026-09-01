"""C3 确定性主编排器（P4/P5）—— 直接连接 C1/B2/B4/C2，替代五模型在线主链。

继承 WorkflowRunner 复用：会话锁接线、_finalize 提交、_build_dual_artifacts、
R-002 临时约束闭环与 P1 性能观测。仅 override ``_run_locked``：

首次推荐（无前文菜单）：
    FastIntentRouter → retrieve → evaluate → generate → 选优 → validate → answer

约束追加（有前文菜单，P5）：
    FastIntentRouter → store 临时约束 → retrieve 补充 → evaluate(当前+补充)
    → generate(locked=安全当前菜) → 选优 → validate → answer

replace/reject/restore 复用已提交菜单与 QueryPlan，走确定性增量链路。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import replace
from typing import Any
from uuid import UUID

from food_agent_v2.c3 import detect_untrusted_instruction
from food_agent_v2.c3.authoritative_answer import AuthoritativeAnswerBuilder
from food_agent_v2.c3.delta_planner import DeltaPlanner
from food_agent_v2.c3.fast_intent import FastIntentRouter, IntentDelta
from food_agent_v2.c3.narrative import NarrativePolisher, narrative_polish_enabled
from food_agent_v2.c3.perf import PerfTrace
from food_agent_v2.c3.query_normalizer import QueryNormalizer
from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.state import (
    NodeType,
    RequestStatus,
    WorkflowError,
    WorkflowState,
    reduce_workflow_state,
)
from food_agent_v2.c3.tool_handler import ToolContext, ToolHandler
from food_agent_v2.c4 import (
    ContextBudgetExceeded,
    ContextService,
    PermanentConstraintLoadFailed,
)
from food_agent_v2.contracts.artifacts import (
    FinalValidationArtifact,
    MenuDecisionArtifact,
    QueryPlanArtifact,
    ReviewArtifact,
)
from food_agent_v2.contracts.build import canonical_json_hash
from food_agent_v2.d1 import api as d1_api


def _semantic_health_exclusions(
    constraints: tuple[str, ...],
    participant_refs: tuple[str, ...],
    exclude_ingredients: tuple[str, ...],
) -> tuple[str, ...]:
    if not constraints:
        return ()
    output: list[str] = []
    for constraint in constraints:
        if constraint.count(":") == 2:
            output.append(constraint)
            continue
        if len(participant_refs) != 1:
            output.append(constraint)
            continue
        participant = participant_refs[0]
        if "过敏" in constraint or "不耐受" in constraint:
            allergy = next(
                (
                    ingredient
                    for ingredient in exclude_ingredients
                    if re.search(
                        rf"{re.escape(ingredient)}\s*(?:过敏|不耐受)"
                        rf"|(?:过敏|不耐受)\s*{re.escape(ingredient)}",
                        constraint,
                    )
                ),
                None,
            )
            if allergy:
                output.append(f"{participant}:过敏:{allergy}")
                continue
            output.append(constraint)
            continue
        taboo = re.search(r"(?:不能吃|别吃)([\u4e00-\u9fff]{1,8})", constraint)
        if taboo:
            output.append(f"{participant}:禁忌:{taboo.group(1)}")
            continue
        disease = next(
            (
                item
                for item in ("糖尿病", "高血压", "痛风", "肾病", "脂肪肝")
                if item in constraint
            ),
            None,
        )
        if disease:
            output.append(f"{participant}:疾病:{disease}")
    return tuple(output)


def _stable_merge(*collections: tuple[str, ...]) -> tuple[str, ...]:
    """按输入顺序合并集合，保留每个值首次出现的位置。"""
    return tuple(dict.fromkeys(
        value for collection in collections for value in collection
    ))


#: generate_feasible_menus 返回的业务终态 → reducer result 映射。
_GENERATE_TERMINAL = {
    "no_safe_menu": "no_safe_menu",
    "no_feasible_menu": "no_feasible_menu",
}


class DeterministicRecommendationOrchestrator(WorkflowRunner):
    """确定性主编排器：首次推荐与多轮菜单操作走确定性链路。"""

    #: 确定性链路中各工具调用的语义节点（回执 node_id，非空即可，供审计信封）。
    _NODE_RETRIEVE = NodeType.QUERY_UNDERSTANDING.value
    _NODE_HEALTH = NodeType.HEALTH_MENU_PLANNING.value
    _NODE_DECISION = NodeType.MENU_DECISION.value

    @staticmethod
    def _has_current_menu(c4: ContextService, session_id: str) -> bool:
        """session 是否已有前文已提交菜单（多轮信号）。

        用 C4 的 MySQL 投影判断；无查询能力（测试替身）视为无前文菜单，
        存储异常则保守 fallback legacy（不冒险走确定性链路丢失多轮上下文）。
        """
        gss = getattr(c4, "get_session_state", None)
        if gss is None:
            return False
        try:
            state = gss(session_id)
        except Exception:
            return True
        return bool(state and state.get("current_menu"))

    @staticmethod
    def _resolve_replace_target(message: str, current_menu: dict) -> int | None:
        matches = {
            int(item["recipe_id"])
            for item in current_menu.get("items", [])
            if str(item.get("name") or "").strip()
            and str(item["name"]).strip() in message
        }
        return next(iter(matches)) if len(matches) == 1 else None

    @staticmethod
    def _replace_previous_plan(previous: dict | None) -> dict | None:
        if not previous:
            return None
        hard_keys = {
            "meal_types", "population_tags", "exclude_ingredients",
            "nutrition_goal_codes", "health_exclusions",
            "time_constraint_seconds", "time_constraint_policy",
            "dish_count_requested",
        }
        return {key: value for key, value in previous.items() if key in hard_keys}

    @staticmethod
    def _previous_menu_version(session_state: dict) -> dict | None:
        history = list(session_state.get("menu_history") or [])
        current_id = (session_state.get("current_menu") or {}).get("plan_id")
        indexes = [
            index for index, item in enumerate(history)
            if item.get("plan_id") == current_id
        ]
        if len(indexes) != 1 or indexes[0] == 0:
            return None
        return history[indexes[0] - 1]

    @staticmethod
    def _restore_intent(snapshot: dict | None, message: str,
                        dish_count: int) -> IntentDelta:
        source = snapshot or {}
        return IntentDelta(
            intent="restore",
            query=str(source.get("rewritten_query") or message),
            rewritten_query=str(source.get("rewritten_query") or message),
            meal_types=tuple(source.get("meal_types") or ()),
            population_tags=tuple(source.get("population_tags") or ()),
            scenario_tags=tuple(source.get("scenario_tags") or ()),
            dish_count_requested=dish_count,
            taste_tags=tuple(source.get("taste_tags") or ()),
            cuisine_tags=tuple(source.get("cuisine_tags") or ()),
            dish_types=tuple(source.get("dish_types") or ()),
            include_ingredients=tuple(source.get("include_ingredients") or ()),
            exclude_ingredients=tuple(source.get("exclude_ingredients") or ()),
            nutrition_goal_codes=tuple(source.get("nutrition_goal_codes") or ()),
            health_exclusions=tuple(source.get("health_exclusions") or ()),
            time_constraint_seconds=source.get("time_constraint_seconds"),
            time_constraint_policy=str(
                source.get("time_constraint_policy") or "flexible"
            ),
        )

    def _build_query_plan(self, intent, request_id: str,
                          participant_refs: list[str]) -> QueryPlanArtifact:
        qp = QueryPlanArtifact(
            artifact_id=uuid.uuid4(),
            request_id=UUID(request_id),
            participant_refs=tuple(participant_refs),
            rewritten_query=intent.rewritten_query or intent.query,
            meal_types=intent.meal_types,
            population_tags=intent.population_tags,
            dish_types=intent.dish_types,
            taste_tags=intent.taste_tags or intent.flavor_preferences,
            cuisine_tags=intent.cuisine_tags,
            scenario_tags=intent.scenario_tags,
            include_ingredients=intent.include_ingredients,
            exclude_ingredients=intent.exclude_ingredients,
            nutrition_goal_codes=intent.nutrition_goal_codes,
            dish_count_requested=intent.dish_count_requested,
            health_exclusions=intent.health_exclusions,
            time_constraint_seconds=intent.time_constraint_seconds,
            time_constraint_policy=intent.time_constraint_policy,
            input_fingerprint=canonical_json_hash(
                {"request_id": request_id, "participant_refs": participant_refs}),
            content_hash="0" * 64,
        )
        return qp.model_copy(update={"content_hash": self._content_hash(qp)})

    @staticmethod
    def _retrieval_query(intent) -> str:
        """检索只消费已经验证的封闭式 rewritten_query。"""
        return intent.rewritten_query or intent.query

    @staticmethod
    def _apply_semantic_rewrite(
        routed: IntentDelta,
        rewrite,
        participant_refs: tuple[str, ...],
        *,
        has_current_menu: bool,
    ) -> IntentDelta:
        action = routed.intent
        if action == "model_fallback":
            action = "add_constraint" if has_current_menu else "new_recommendation"
        meal_types = _stable_merge(
            (routed.meal_type,) if routed.meal_type else (),
            routed.meal_types,
            rewrite.meal_types,
        )
        population_tags = _stable_merge(
            routed.population_tags,
            rewrite.population_tags,
        )
        scenario_tags = _stable_merge(
            (routed.scenario,) if routed.scenario else (),
            routed.scenario_tags,
            rewrite.scenario_tags,
        )
        taste_tags = _stable_merge(
            routed.flavor_preferences,
            routed.taste_tags,
            rewrite.taste_tags,
        )
        cuisine_tags = _stable_merge(routed.cuisine_tags, rewrite.cuisine_tags)
        dish_types = _stable_merge(routed.dish_types, rewrite.dish_types)
        include_ingredients = _stable_merge(
            routed.include_ingredients,
            rewrite.include_ingredients,
        )
        exclude_ingredients = _stable_merge(
            routed.exclude_ingredients,
            rewrite.exclude_ingredients,
        )
        nutrition_goal_codes = _stable_merge(
            routed.nutrition_goal_codes,
            rewrite.nutrition_goal_codes,
        )
        health_exclusions = routed.health_exclusions
        if not health_exclusions:
            health_exclusions = _semantic_health_exclusions(
                rewrite.health_constraints,
                participant_refs,
                exclude_ingredients,
            )
        router_hard_time = (
            routed.time_constraint_policy == "hard"
            and routed.time_constraint_seconds is not None
        )
        return replace(
            routed,
            intent=action,
            rewritten_query=rewrite.retrieval_query,
            meal_type=meal_types[0] if meal_types else None,
            meal_types=meal_types,
            population_tags=population_tags,
            scenario=scenario_tags[0] if scenario_tags else None,
            scenario_tags=scenario_tags,
            dish_count_requested=(
                routed.dish_count_requested
                if routed.dish_count_requested is not None
                else rewrite.dish_count
            ),
            flavor_preferences=taste_tags,
            taste_tags=taste_tags,
            cuisine_tags=cuisine_tags,
            dish_types=dish_types,
            include_ingredients=include_ingredients,
            exclude_ingredients=exclude_ingredients,
            nutrition_goal_codes=nutrition_goal_codes,
            health_exclusions=health_exclusions,
            time_constraint_seconds=(
                routed.time_constraint_seconds
                if router_hard_time else rewrite.time_constraint_seconds
            ),
            time_constraint_policy=(
                "hard"
                if router_hard_time or rewrite.max_time_minutes is not None
                else "flexible"
            ),
        )

    def _guard_active(self, state, c4, session_id, lock_token, lost):
        """取消/失锁统一门卫：返回终态 state（若触发）或 None（继续）。

        在确定性链路的每个阶段边界与原子提交前调用；取消后不得再调用成功工具
        或提交菜单/回答，失锁（fencing token 过期）返回精确 SESSION_LOCK_LOST。
        """
        if self._is_cancelled(state.request_id):
            return reduce_workflow_state(
                state, action="set_status", status=RequestStatus.CANCELLED)
        if lost.is_set() or not self._session_lock_held(c4, session_id, lock_token):
            return self._fail(state, "SESSION_LOCK_LOST", "会话锁已失效")
        return None

    def _finalize_clarification(self, request_id, session_id, participants,
                                c4, lock_token, intent):
        """澄清终态：构造 needs_clarification state 并 finalize（发布澄清事件）。"""
        build_id = self._resolve_build_id()
        participant_refs = [p["participant_ref"] for p in participants]
        state = WorkflowState(
            request_id=request_id, build_id=build_id,
            status=RequestStatus.RUNNING, participant_refs=participant_refs)
        state = reduce_workflow_state(
            state, action="query_understanding", needs_clarification=True)
        state.error = WorkflowError(
            "NEEDS_CLARIFICATION",
            intent.clarification_reason or "需求需要进一步澄清")
        self._finalize(state, request_id, c4, lock_token)

    # ---- context_building（首次推荐与约束追加共用）----

    def _build_context(self, state, tool_ctx, message, participant_refs,
                       user_id_mapping, request_id, session_id, build_id, c4):
        """返回 (state, ok)；ok=False 时 state 已终态，调用方 finalize。"""
        state = self._enter_node(state, tool_ctx, NodeType.CONTEXT_BUILDING)
        self._trace.mark_node_start(NodeType.CONTEXT_BUILDING.value)

        _injection = detect_untrusted_instruction(message)
        if _injection:
            return self._fail(state, "UNTRUSTED_INSTRUCTION_DETECTED",
                              f"检测到指令注入: {_injection}"), False

        try:
            ctx, _manifest = c4.build_shared_context(
                session_id, participant_refs,
                {"raw_text": message, "timestamp": time.time()},
                user_id_mapping, request_id=request_id, build_id=build_id,
            )
        except PermanentConstraintLoadFailed as exc:
            return self._fail(state, "PERMANENT_CONSTRAINT_LOAD_FAILED", str(exc)), False
        except ContextBudgetExceeded as exc:
            return self._fail(state, "CONTEXT_BUDGET_EXCEEDED", str(exc)), False
        state = reduce_workflow_state(
            state, action="set_context_ref", shared_context_ref=ctx.session_id)

        integrity = c4.validate_context_integrity(state.shared_context_ref)
        if not integrity.get("valid", False):
            return self._fail(state, "CONTEXT_INTEGRITY_FAILED",
                              f"上下文完整性校验失败: {integrity.get('reason', 'core mismatch')}"), False

        d1_api.publish_analysis_event(request_id, "context_ready",
                                      f"已理解{len(participant_refs)}位参与者的需求", [])
        # answer_started：尽早发送不承诺菜单的开场（首 Token 计时，L2）
        d1_api.publish_answer_started(request_id)
        state = reduce_workflow_state(state, action="context_building", manifest_valid=True)
        self._trace.mark_node_end(NodeType.CONTEXT_BUILDING.value)
        return state, True

    # ---- 公共：从已生成的 feasible_menus 到终态 state ----

    def _select_validate_answer(self, state, tool_ctx, plans, request_id,
                                participant_refs, c4, session_id, lock_token, lost,
                                *, allow_polish: bool = True):
        """选优 → 最终校验 → MenuDecision → 确定性回答 → 回执 → 终态 state。"""
        handler = ToolHandler(tool_ctx)

        # 构建双 Artifact（HealthEvaluationArtifact + FeasibleMenuArtifact）
        state, feasible_artifact = self._build_dual_artifacts(state, tool_ctx)
        if state.is_terminal():
            return state
        self._trace.mark_node_end(NodeType.HEALTH_MENU_PLANNING.value)

        # 确定性选优 + 最终校验
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            return guard
        self._trace.mark_node_start(NodeType.MENU_DECISION.value)
        tool_ctx.node_id = self._NODE_DECISION
        best = sorted(
            plans, key=lambda p: (-getattr(p, "total_score", 0.0),
                                  getattr(p, "plan_id", "")))[0]
        handler.execute("validate_selected_menu_health",
                        {"plan_id": best.plan_id, "recipe_ids": list(best.recipe_ids)})
        fv = tool_ctx.previous_results.get("final_validation")
        if not isinstance(fv, FinalValidationArtifact):
            return self._fail(state, "FINAL_HEALTH_VALIDATION_FAILED",
                              "缺少 B4 最终健康校验结果")
        if fv.status != "PASS":
            return self._fail(state, "FINAL_HEALTH_VALIDATION_FAILED",
                              f"最终校验 verdict: {fv.status}")

        # MenuDecisionArtifact（确定性选优，绑定可行方案与最终校验）
        md = MenuDecisionArtifact(
            artifact_id=uuid.uuid4(),
            request_id=UUID(request_id),
            plan_id=fv.plan_id,
            recipe_ids=tuple(fv.recipe_ids),
            menu_hash=fv.menu_hash,
            feasible_menu_artifact_ref=str(feasible_artifact.artifact_id),
            final_validation_ref=str(fv.artifact_id),
            participant_refs=tuple(participant_refs),
            content_hash="0" * 64,
        )
        md = md.model_copy(update={"content_hash": self._content_hash(md)})
        self._trace.mark_node_end(NodeType.MENU_DECISION.value)

        # 确定性回答 + ReviewArtifact(PASS)
        self._trace.mark_node_start(NodeType.ANSWER_GENERATION.value)
        estimated_minutes = max(
            1,
            round(best.estimated_makespan_seconds / 60),
        )
        answer = AuthoritativeAnswerBuilder.build(
            md,
            fv,
            state.build_id,
            time_note=f"按当前步骤估算，预计需要 {estimated_minutes} 分钟。",
        )
        # 可选润色（默认关闭；剩余预算 ≥2.5s 才启用，最多 2.0s，失败回退）
        if allow_polish and narrative_polish_enabled() and self._trace is not None:
            elapsed = time.perf_counter() - self._trace.processing_started_at
            if elapsed < 5.5:  # 8s 总预算 − 2.5s 润色余量
                answer = NarrativePolisher().polish(
                    answer, timeout_seconds=2.0, trace=self._trace)
        self._trace.mark_node_end(NodeType.ANSWER_GENERATION.value)
        rv = ReviewArtifact(
            artifact_id=uuid.uuid4(),
            request_id=UUID(request_id),
            status="PASS",
            content_hash="0" * 64,
        )

        state = reduce_workflow_state(
            state, action="set_artifact", artifact="menu_decision", value=md)
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="final_validation", value=fv)
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="answer", value=answer)
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="review", value=rv)
        # 记录工具回执（审计信封要求 tool_receipt_refs 与 tool_input_output_hashes 非空对应）
        state = reduce_workflow_state(
            state, action="record_receipts",
            receipts=self._as_authoritative_receipts(tool_ctx.tool_receipts))
        state = reduce_workflow_state(state, action="unified_review", status="PASS")
        # 提交前最后门卫：取消/失锁后不得提交菜单或回答
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            return guard
        state = reduce_workflow_state(state, action="atomic_commit")
        return state

    # ---- 入口 ----

    def _run_locked(self, request_id: str, session_id: str,
                    message: str, participants: list[dict],
                    config: dict | None, c4: ContextService,
                    lock_token: str, lost: Any) -> None:
        if self._trace is None:
            self._trace = PerfTrace(request_id=request_id)

        participant_refs = [p["participant_ref"] for p in participants]
        intent = FastIntentRouter.route(message, tuple(participant_refs))

        # needs_clarification/conflict → 澄清终态（不进入推荐链路）
        if intent.intent in ("needs_clarification", "conflict"):
            self._finalize_clarification(request_id, session_id, participants,
                                         c4, lock_token, intent)
            return

        if intent.intent in ("replace", "restore"):
            session_state = c4.get_session_state(session_id) or {}
            current_menu = session_state.get("current_menu") or {}
            if not current_menu:
                self._finalize_clarification(
                    request_id, session_id, participants, c4, lock_token,
                    replace(intent, clarification_reason="当前没有可操作的菜单"),
                )
                return
            if intent.intent == "restore":
                previous_menu = self._previous_menu_version(session_state)
                if previous_menu is None:
                    self._finalize_clarification(
                        request_id, session_id, participants, c4, lock_token,
                        replace(intent, clarification_reason="没有可恢复的上一版菜单"),
                    )
                    return
                self._run_restore(
                    request_id, session_id, message, participants, c4,
                    lock_token, lost, session_state, previous_menu,
                )
                return
            target_recipe_id = self._resolve_replace_target(message, current_menu)
            if target_recipe_id is None:
                self._finalize_clarification(
                    request_id, session_id, participants, c4, lock_token,
                    replace(intent, clarification_reason="请明确要替换的当前菜名"),
                )
                return
            rewrite = QueryNormalizer(self._llm).normalize(
                message,
                tuple(participant_refs),
                previous_query_plan=self._replace_previous_plan(
                    session_state.get("query_plan")
                ),
            )
            intent = self._apply_semantic_rewrite(
                replace(intent, target_recipe_id=target_recipe_id),
                rewrite,
                tuple(participant_refs),
                has_current_menu=True,
            )
            self._run_delta(request_id, session_id, message, participants,
                            config, c4, lock_token, lost, intent)
            return

        has_current_menu = self._has_current_menu(c4, session_id)
        previous_query_plan = None
        if has_current_menu and intent.intent in ("add_constraint", "reject_plan"):
            session_state = c4.get_session_state(session_id) or {}
            previous_query_plan = session_state.get("query_plan")
        rewrite = QueryNormalizer(self._llm).normalize(
            message,
            tuple(participant_refs),
            previous_query_plan=previous_query_plan,
        )
        intent = self._apply_semantic_rewrite(
            intent,
            rewrite,
            tuple(participant_refs),
            has_current_menu=has_current_menu,
        )

        # add_constraint / reject_plan 且已有前文菜单 → 确定性 delta（最小修改）
        if intent.intent in ("add_constraint", "reject_plan") and has_current_menu:
            self._run_delta(request_id, session_id, message, participants,
                            config, c4, lock_token, lost, intent)
            return
        # new_recommendation（或 add_constraint/reject 但无前文菜单）→ 全新首次推荐

        # 首次推荐（P4）。
        build_id = self._resolve_build_id()
        user_id_mapping = {p["participant_ref"]: int(p["user_id"]) for p in participants}
        state = WorkflowState(
            request_id=request_id, build_id=build_id,
            status=RequestStatus.RUNNING, participant_refs=participant_refs)
        tool_ctx = ToolContext(
            request_id=request_id, build_id=build_id,
            participant_user_mapping=user_id_mapping,
            session_id=session_id, context_service=c4)

        state, ok = self._build_context(state, tool_ctx, message, participant_refs,
                                        user_id_mapping, request_id, session_id,
                                        build_id, c4)
        if not ok:
            self._finalize(state, request_id, c4, lock_token)
            return

        state = self._deterministic_chain(
            state, tool_ctx, intent, request_id, session_id, participant_refs,
            user_id_mapping, c4, lock_token, lost)
        self._finalize(state, request_id, c4, lock_token)

    def _deterministic_chain(self, state, tool_ctx, intent, request_id,
                             session_id, participant_refs, user_id_mapping, c4,
                             lock_token, lost):
        handler = ToolHandler(tool_ctx)

        # 1. QueryPlanArtifact（确定性意图 → 结构化查询计划）
        self._trace.mark_node_start(NodeType.QUERY_UNDERSTANDING.value)
        qp = self._build_query_plan(intent, request_id, participant_refs)
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="query_plan", value=qp)
        tool_ctx.previous_results["query_plan"] = qp
        if getattr(qp, "health_exclusions", ()):
            state = self._handle_query_plan_exclusions(
                state, qp, session_id, c4, user_id_mapping)
            if state.is_terminal():
                return state
        if qp.time_constraint_policy == "hard" and qp.time_constraint_seconds:
            tool_ctx.max_estimated_time_seconds = qp.time_constraint_seconds
        d1_api.publish_analysis_event(request_id, "query_understanding",
                                      "理解需求完成", [])
        self._trace.mark_node_end(NodeType.QUERY_UNDERSTANDING.value)

        # 2. 检索
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            return guard
        self._trace.mark_node_start("retrieval")
        tool_ctx.node_id = self._NODE_RETRIEVE
        handler.execute("retrieve_recipes",
                        {"query": self._retrieval_query(intent), "top_k": 40})
        self._trace.mark_node_end("retrieval")
        retrieval = tool_ctx.previous_results.get("retrieval")
        candidate_ids = [c.recipe_id for c in getattr(retrieval, "candidates", []) or []]
        if not candidate_ids:
            return self._fail(state, "CANDIDATES_REQUIRED", "检索无候选")

        # 3. 健康审查
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            return guard
        self._trace.mark_node_start(NodeType.HEALTH_MENU_PLANNING.value)
        tool_ctx.node_id = self._NODE_HEALTH
        eval_result = handler.execute("evaluate_recipe_health", {"recipe_ids": candidate_ids})
        if isinstance(eval_result, dict) and "error" in eval_result:
            self._trace.mark_node_end(NodeType.HEALTH_MENU_PLANNING.value)
            return self._fail(state, "TOOL_EXECUTION_FAILED",
                              f"健康审查失败: {eval_result.get('error')}")
        health = tool_ctx.previous_results.get("health_evaluation")
        safe_ids = list(getattr(health, "safe_recipe_ids", []) or [])
        if not safe_ids:
            d1_api.publish_analysis_event(request_id, "health_evaluation",
                                          "健康审查完成（无安全候选）", [])
            return reduce_workflow_state(
                state, action="health_menu_planning", result="no_safe_menu")
        d1_api.publish_analysis_event(request_id, "health_evaluation",
                                      "健康审查完成", [])

        # 4. 生成可行菜单
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            return guard
        gen = handler.execute("generate_feasible_menus",
                              {"safe_recipe_ids": safe_ids,
                               "dish_count": qp.dish_count_requested})
        if isinstance(gen, dict) and "error" in gen:
            return self._fail(state, "TOOL_EXECUTION_FAILED",
                              f"菜单生成失败: {gen['error']}")
        plans = tool_ctx.previous_results.get("feasible_menus", [])
        if not plans:
            note = gen.get("note") if isinstance(gen, dict) else None
            result = _GENERATE_TERMINAL.get(note, "no_feasible_menu")
            d1_api.publish_analysis_event(request_id, "menu_planning",
                                          f"菜单方案生成终态: {result}", [])
            return reduce_workflow_state(
                state, action="health_menu_planning", result=result)
        d1_api.publish_analysis_event(request_id, "menu_planning",
                                      "菜单方案生成完成", [])

        # 5. 选优 + 校验 + 回答 + 提交（公共 helper）
        return self._select_validate_answer(
            state, tool_ctx, plans, request_id, participant_refs,
            c4, session_id, lock_token, lost)

    # ---- L1.3：多轮 delta（约束追加/方案否定，在已有菜单上最小修改）----

    def _run_restore(self, request_id, session_id, message, participants, c4,
                     lock_token, lost, session_state, previous_menu):
        build_id = self._resolve_build_id()
        participant_refs = [p["participant_ref"] for p in participants]
        user_id_mapping = {p["participant_ref"]: int(p["user_id"]) for p in participants}
        state = WorkflowState(
            request_id=request_id, build_id=build_id,
            status=RequestStatus.RUNNING, participant_refs=participant_refs)
        tool_ctx = ToolContext(
            request_id=request_id, build_id=build_id,
            participant_user_mapping=user_id_mapping,
            session_id=session_id, context_service=c4)

        state, ok = self._build_context(
            state, tool_ctx, message, participant_refs, user_id_mapping,
            request_id, session_id, build_id, c4)
        if not ok:
            self._finalize(state, request_id, c4, lock_token)
            return

        recipe_ids = [int(value) for value in previous_menu.get("recipe_ids", [])]
        snapshot = previous_menu.get("query_plan") or session_state.get("query_plan")
        qp = self._build_query_plan(
            self._restore_intent(snapshot, message, len(recipe_ids)),
            request_id,
            participant_refs,
        )
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="query_plan", value=qp)
        tool_ctx.previous_results["query_plan"] = qp
        if qp.health_exclusions:
            state = self._handle_query_plan_exclusions(
                state, qp, session_id, c4, user_id_mapping)
            if state.is_terminal():
                self._finalize(state, request_id, c4, lock_token)
                return
        if qp.time_constraint_policy == "hard" and qp.time_constraint_seconds:
            tool_ctx.max_estimated_time_seconds = qp.time_constraint_seconds

        from food_agent_v2.b3.recipe_views import get_view_builder

        builder = get_view_builder()
        if (not recipe_ids or len(set(recipe_ids)) != len(recipe_ids)
                or any(builder.build_retrieval_view(rid) is None for rid in recipe_ids)):
            self._finalize(
                self._fail(
                    state,
                    "RESTORE_VERSION_UNAVAILABLE",
                    "上一版菜单在当前构建中不可用",
                ),
                request_id, c4, lock_token,
            )
            return

        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            self._finalize(guard, request_id, c4, lock_token)
            return
        handler = ToolHandler(tool_ctx)
        tool_ctx.node_id = self._NODE_HEALTH
        evaluated = handler.execute(
            "evaluate_recipe_health", {"recipe_ids": recipe_ids})
        if isinstance(evaluated, dict) and "error" in evaluated:
            self._finalize(
                self._fail(state, "TOOL_EXECUTION_FAILED",
                           f"健康审查失败: {evaluated.get('error')}"),
                request_id, c4, lock_token,
            )
            return
        health = tool_ctx.previous_results.get("health_evaluation")
        safe_ids = [int(value) for value in getattr(health, "safe_recipe_ids", ())]
        if set(safe_ids) != set(recipe_ids):
            self._finalize(reduce_workflow_state(
                state, action="health_menu_planning", result="no_safe_menu"),
                request_id, c4, lock_token)
            return

        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            self._finalize(guard, request_id, c4, lock_token)
            return
        generated = handler.execute("generate_feasible_menus", {
            "safe_recipe_ids": recipe_ids,
            "locked_recipe_ids": recipe_ids,
        })
        if isinstance(generated, dict) and "error" in generated:
            self._finalize(
                self._fail(state, "TOOL_EXECUTION_FAILED",
                           f"菜单生成失败: {generated.get('error')}"),
                request_id, c4, lock_token,
            )
            return
        expected = set(recipe_ids)
        plans = [
            plan for plan in tool_ctx.previous_results.get("feasible_menus", [])
            if set(plan.recipe_ids) == expected and len(plan.recipe_ids) == len(recipe_ids)
        ]
        if not plans:
            self._finalize(reduce_workflow_state(
                state, action="health_menu_planning", result="no_feasible_menu"),
                request_id, c4, lock_token)
            return
        for plan in plans:
            plan.recipe_ids = list(recipe_ids)
        tool_ctx.previous_results["feasible_menus"] = plans
        state = self._select_validate_answer(
            state, tool_ctx, plans, request_id, participant_refs,
            c4, session_id, lock_token, lost, allow_polish=False)
        self._finalize(state, request_id, c4, lock_token)

    def _run_delta(self, request_id, session_id, message,
                   participants, config, c4, lock_token, lost, intent):
        build_id = self._resolve_build_id()
        participant_refs = [p["participant_ref"] for p in participants]
        user_id_mapping = {p["participant_ref"]: int(p["user_id"]) for p in participants}
        state = WorkflowState(
            request_id=request_id, build_id=build_id,
            status=RequestStatus.RUNNING, participant_refs=participant_refs)
        tool_ctx = ToolContext(
            request_id=request_id, build_id=build_id,
            participant_user_mapping=user_id_mapping,
            session_id=session_id, context_service=c4)

        state, ok = self._build_context(state, tool_ctx, message, participant_refs,
                                        user_id_mapping, request_id, session_id,
                                        build_id, c4)
        if not ok:
            self._finalize(state, request_id, c4, lock_token)
            return

        # 1. 用已路由的 intent 构建查询计划并写入临时约束（R-002 闭环）
        qp = self._build_query_plan(intent, request_id, participant_refs)
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="query_plan", value=qp)
        tool_ctx.previous_results["query_plan"] = qp
        if getattr(qp, "health_exclusions", ()):
            state = self._handle_query_plan_exclusions(
                state, qp, session_id, c4, user_id_mapping)
            if state.is_terminal():
                self._finalize(state, request_id, c4, lock_token)
                return

        # 2. 加载当前菜单
        current = (c4.get_session_state(session_id) or {}).get("current_menu") or {}
        current_ids = list(current.get("recipe_ids", []))
        if not current_ids:
            # 无前文菜单（理论上不会，因 _has_current_menu 已判断）→ 降级首次推荐
            state = self._deterministic_chain(
                state, tool_ctx, intent, request_id, session_id, participant_refs,
                user_id_mapping, c4, lock_token, lost)
            self._finalize(state, request_id, c4, lock_token)
            return

        # 约束追加/否定保持当前菜数（作为 query_plan 权威，覆盖 None 菜数）
        qp = qp.model_copy(update={"dish_count_requested": len(current_ids)})
        tool_ctx.previous_results["query_plan"] = qp
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="query_plan", value=qp)

        handler = ToolHandler(tool_ctx)

        # 3. 检索补充候选（替换违规菜的来源）
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            self._finalize(guard, request_id, c4, lock_token)
            return
        tool_ctx.node_id = self._NODE_RETRIEVE
        handler.execute("retrieve_recipes",
                        {"query": self._retrieval_query(intent), "top_k": 40})
        retrieval = tool_ctx.previous_results.get("retrieval")
        candidate_ids = [c.recipe_id for c in getattr(retrieval, "candidates", []) or []]
        all_ids = list(dict.fromkeys(current_ids + [r for r in candidate_ids
                                                    if r not in current_ids]))

        # 4. 一次性审查（当前菜 + 补充候选），用追加后的约束
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            self._finalize(guard, request_id, c4, lock_token)
            return
        self._trace.mark_node_start(NodeType.HEALTH_MENU_PLANNING.value)
        tool_ctx.node_id = self._NODE_HEALTH
        eval_result = handler.execute("evaluate_recipe_health", {"recipe_ids": all_ids})
        if isinstance(eval_result, dict) and "error" in eval_result:
            self._trace.mark_node_end(NodeType.HEALTH_MENU_PLANNING.value)
            self._finalize(self._fail(state, "TOOL_EXECUTION_FAILED",
                                      f"健康审查失败: {eval_result.get('error')}"),
                           request_id, c4, lock_token)
            return
        health = tool_ctx.previous_results.get("health_evaluation")
        safe_ids = list(getattr(health, "safe_recipe_ids", []) or [])

        # 5. DeltaPlanner 计算最小修改（锁定安全当前菜 / 拒绝否定菜）
        delta = DeltaPlanner().plan(
            current_ids,
            IntentDelta(
                intent=intent.intent,
                target_recipe_id=intent.target_recipe_id,
            ),
            safe_ids,
        )
        locked = list(delta.locked_recipe_ids)
        rejected = list(delta.rejected_recipe_ids)
        if not safe_ids:
            d1_api.publish_analysis_event(request_id, "health_evaluation",
                                          "健康审查完成（无安全候选）", [])
            self._finalize(reduce_workflow_state(
                state, action="health_menu_planning", result="no_safe_menu"),
                request_id, c4, lock_token)
            return
        d1_api.publish_analysis_event(request_id, "health_evaluation",
                                      "健康审查完成", [])

        # 6. 生成新菜单（锁定安全当前菜，补足/替换违规菜）
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            self._finalize(guard, request_id, c4, lock_token)
            return
        gen = handler.execute("generate_feasible_menus", {
            "safe_recipe_ids": safe_ids,
            "locked_recipe_ids": locked,
            "rejected_recipe_ids": rejected,
        })
        if isinstance(gen, dict) and "error" in gen:
            self._finalize(self._fail(state, "TOOL_EXECUTION_FAILED",
                                      f"菜单生成失败: {gen['error']}"),
                           request_id, c4, lock_token)
            return
        plans = tool_ctx.previous_results.get("feasible_menus", [])
        if not plans:
            note = gen.get("note") if isinstance(gen, dict) else None
            result = _GENERATE_TERMINAL.get(note, "no_feasible_menu")
            d1_api.publish_analysis_event(request_id, "menu_planning",
                                          f"菜单方案生成终态: {result}", [])
            self._finalize(reduce_workflow_state(
                state, action="health_menu_planning", result=result),
                request_id, c4, lock_token)
            return
        d1_api.publish_analysis_event(request_id, "menu_planning",
                                      "菜单方案生成完成", [])

        # 7. 选优 + 校验 + 回答 + 提交（公共 helper）
        state = self._select_validate_answer(
            state, tool_ctx, plans, request_id, participant_refs,
            c4, session_id, lock_token, lost)
        self._finalize(state, request_id, c4, lock_token)
