"""C3 LangGraph 混合编排器 —— 遵循 ADR-0008 与受约束的 Agent 决策循环。

基于 LangGraph StateGraph 构建显式编排图：
1. understand_intent: 意图路由、上下文构建、注入防御与查询计划生成；
2. search_candidates: 调用 C1 检索工具获取候选；
3. audit_recipe_health: 调用 B4 健康审查工具进行严格全员禁忌与疾病过滤；
4. combine_nutritional_menu: 调用 C2 营养组合工具，严格保证零静默放宽（Zero Silent Relaxation）；
   若无法满足用户硬约束（菜数不足、耗时超标、无解），计算精确诊断，转入 inquire_user；
5. inquire_user: 诚实归因并主动提供 2~3 个结构化选项，进入 needs_clarification；
6. validate_and_answer: 最终健康校验、构建权威回答与审查；
7. commit: 事务性提交与状态终态处理。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import replace
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from food_agent_v2.c2 import MenuHardConstraints
from food_agent_v2.c3 import detect_untrusted_instruction
from food_agent_v2.c3.agent_actions import (
    ActionType,
    AgentAction,
    Observation,
)
from food_agent_v2.c3.agent_policy import AgentPolicy
from food_agent_v2.c3.agent_prompts import (
    AGENT_DECISION_SYSTEM_PROMPT,
    format_agent_prompt,
)
from food_agent_v2.c3.authoritative_answer import AuthoritativeAnswerBuilder
from food_agent_v2.c3.delta_planner import DeltaPlanner
from food_agent_v2.c3.fast_intent import FastIntentRouter, IntentDelta
from food_agent_v2.c3.intent_helpers import (
    _semantic_health_exclusions,
    _stable_merge,
)
from food_agent_v2.c3.narrative import NarrativePolisher, narrative_polish_enabled
from food_agent_v2.c3.perf import PerfTrace
from food_agent_v2.c3.query_normalizer import QueryNormalizer
from food_agent_v2.c3.runtime import AgentRuntime
from food_agent_v2.c3.state import (
    NodeType,
    RequestStatus,
    WorkflowError,
    WorkflowState,
    reduce_workflow_state,
)
from food_agent_v2.c3.tool_handler import ToolContext, ToolHandler
from food_agent_v2.c3.tools import (
    audit_recipe_health,
    combine_nutritional_menu,
    search_candidates,
)
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

logger = logging.getLogger(__name__)


class ModelDecisionError(Exception):
    """Agent 决策模型异常（用于显式终止决策并进入 fail-closed 终态，消除静默降级）。"""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class DeterministicScriptedAgentModel:
    """显式注入的确定性脚本化 Agent 模型，供测试或显式仿真使用。

    根据观察历史确定性选择下一个动作。生产环境中严禁在模型异常时隐式代跑。
    """

    def __init__(
        self,
        orchestrator: Any | None = None,
    ) -> None:
        self.orchestrator = orchestrator

    def decide_action(self, state: DietAgentState) -> AgentAction:
        if self.orchestrator is not None:
            return self.orchestrator._default_agent_decision(state)
        return LangGraphRecommendationOrchestrator._default_agent_decision(state)


class DietAgentState(TypedDict, total=False):
    """LangGraph 执行视图状态。"""

    request_id: str
    session_id: str
    message: str
    participants: list[dict]
    participant_refs: list[str]
    user_id_mapping: dict[str, int]
    build_id: str
    lock_token: str
    lost: Any
    c4: ContextService
    config: dict[str, Any] | None

    workflow_state: WorkflowState
    tool_context: ToolContext

    intent: IntentDelta | None
    query_plan: QueryPlanArtifact | None
    candidate_recipes: list[int]
    retrieval_result: Any

    safe_recipe_ids: list[int]
    excluded_recipe_ids: list[int]
    health_evaluation: Any
    health_excluded_details: dict[int, list[str]]

    locked_recipe_ids: list[int]
    rejected_recipe_ids: list[int]

    feasible_menus: list[Any]
    selected_menu: Any | None
    final_validation: FinalValidationArtifact | None
    menu_decision: MenuDecisionArtifact | None
    answer: Any | None

    inquiry_needed: bool
    inquiry_reason: str
    inquiry_options: list[str]
    structured_options: list[dict]
    diagnosis_code: str
    diagnostics: dict[str, Any]

    error_code: str | None
    error_message: str | None
    is_terminal: bool

    # Agent 行动—观察循环字段
    policy: AgentPolicy
    observations: list[Observation]
    current_action: AgentAction | None
    execution_context: dict[str, Any]
    pending_clarification_to_consume: str | None
    clarification_response: dict | None
    clarification_transition: Any | None
    selected_option_id: int | None
    supersede_clarification: bool
    clarification_revision: int | None
    is_v2: bool


_UNSET = object()


class LangGraphRecommendationOrchestrator(AgentRuntime):
    """基于 LangGraph 的受约束 Agent 主编排器。"""

    _NODE_RETRIEVE = NodeType.QUERY_UNDERSTANDING.value
    _NODE_HEALTH = NodeType.HEALTH_MENU_PLANNING.value
    _NODE_DECISION = NodeType.MENU_DECISION.value

    def __init__(
        self,
        llm: Any = _UNSET,
        *,
        build_id: str | None = None,
        build_provider: Any | None = None,
        c4: Any = None,
    ) -> None:
        if llm is None:
            super().__init__(
                llm=None,
                build_id=build_id,
                build_provider=build_provider,
                c4=c4,
            )
            self._llm = None
        elif llm is _UNSET:
            super().__init__(
                build_id=build_id,
                build_provider=build_provider,
                c4=c4,
            )
        else:
            super().__init__(
                llm=llm,
                build_id=build_id,
                build_provider=build_provider,
                c4=c4,
            )
        self._graph = self._build_graph()

    # ---- 基础辅助 ----

    @staticmethod
    def _has_current_menu(c4: ContextService, session_id: str) -> bool:
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
    def _restore_intent(
        snapshot: dict | None, message: str, dish_count: int
    ) -> IntentDelta:
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

    @staticmethod
    def _resolve_option_selection(
        message: str, options: list[dict]
    ) -> tuple[dict | None, str]:
        """解析用户回复是否匹配待澄清选项（R5）。

        返回值: (selected_option, status)
        status:
        - "matched": 匹配成功，selected_option 为选项字典
        - "ambiguous": 意图选择选项但存在歧义或语义不明
        - "out_of_range": 选项编号超出范围
        - "not_an_option": 未在选择选项（可能是完全不同的新需求）
        """
        import re

        msg = message.strip()
        num_map = {
            "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
            "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
            "陆": 6, "柒": 7, "捌": 8, "玖": 9, "拾": 10,
        }

        # 1. 歧义词排查
        if any(w in msg for w in ("随便", "都行", "全部", "任选", "都可以", "两样都", "随便选")):
            return None, "ambiguous"

        clean_msg = msg.rstrip("。！？.!? \t")

        # 2. 精确纯数字/中文数字（如 "1", "2", "二", "9"）
        if clean_msg in num_map:
            idx = num_map[clean_msg]
            if 1 <= idx <= len(options):
                return options[idx - 1], "matched"
            else:
                return None, "out_of_range"

        # 3. 严格整句序号模式匹配（必须整句匹配，禁止从任意普通句子中抽取子串数字）
        strict_patterns = [
            r"^(?:请?[选按]|选择|采用|接受)?\s*(?:第|选项)?\s*([0-9]{1,2}|[一二两三四五六七八九十壹贰叁肆伍陆柒捌玖拾])\s*(?:个|项|种|条)?(?:\s*吧|\s*来)?$",
            r"^选项\s*([0-9]{1,2}|[一二两三四五六七八九十壹贰叁肆伍陆柒捌玖拾])$",
            r"^第\s*([0-9]{1,2}|[一二两三四五六七八九十壹贰叁肆伍陆柒捌玖拾])\s*(?:个|项|种|条)?$",
        ]
        for pat in strict_patterns:
            m = re.match(pat, clean_msg)
            if m:
                val = m.group(1)
                if val in num_map:
                    idx = num_map[val]
                    if 1 <= idx <= len(options):
                        return options[idx - 1], "matched"
                    else:
                        return None, "out_of_range"

        # 4. 选项全文精确匹配（与当前活跃问题的某一项 option.text 完全一致，去除标点首尾空白）
        matched_by_text = [
            opt for opt in options
            if clean_msg == opt.get("text", "").strip().rstrip("。！？.!? \t")
        ]
        if len(matched_by_text) == 1:
            return matched_by_text[0], "matched"
        elif len(matched_by_text) > 1:
            return None, "ambiguous"

        # 其他任意文本判定为全新业务需求，由调用方触发旧问题作废（supersede）逻辑
        return None, "not_an_option"

    def _build_query_plan(
        self, intent: Any, request_id: str, participant_refs: list[str]
    ) -> QueryPlanArtifact:
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
                {"request_id": request_id, "participant_refs": participant_refs}
            ),
            content_hash="0" * 64,
        )
        return qp.model_copy(update={"content_hash": self._content_hash(qp)})

    @staticmethod
    def _retrieval_query(intent: Any) -> str:
        return intent.rewritten_query or intent.query

    @staticmethod
    def _apply_semantic_rewrite(
        routed: IntentDelta,
        rewrite: Any,
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

    def _guard_active(
        self, state: WorkflowState, c4: ContextService, session_id: str,
        lock_token: str, lost: Any,
    ) -> WorkflowState | None:
        if self._is_cancelled(state.request_id):
            return reduce_workflow_state(
                state, action="set_status", status=RequestStatus.CANCELLED
            )
        if lost.is_set() or not self._session_lock_held(c4, session_id, lock_token):
            return self._fail(state, "SESSION_LOCK_LOST", "会话锁已失效")
        return None

    def _trace_start(self, node: str) -> None:
        if self._trace is not None:
            self._trace.mark_node_start(node)

    def _trace_end(self, node: str) -> None:
        if self._trace is not None:
            self._trace.mark_node_end(node)

    def _select_validate_answer(
        self,
        state: WorkflowState,
        tool_ctx: ToolContext,
        plans: list[Any],
        request_id: str,
        participant_refs: list[str],
        c4: ContextService,
        session_id: str,
        lock_token: str,
        lost: Any,
        *,
        allow_polish: bool = True,
    ) -> WorkflowState:
        # 构建双 Artifact（HealthEvaluationArtifact + FeasibleMenuArtifact）
        state, feasible_artifact = self._build_dual_artifacts(state, tool_ctx)
        if state.is_terminal():
            return state
        self._trace_end(NodeType.HEALTH_MENU_PLANNING.value)

        # 选优 + 最终校验
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            return guard
        self._trace_start(NodeType.MENU_DECISION.value)
        tool_ctx.node_id = self._NODE_DECISION
        fv = tool_ctx.previous_results.get("final_validation")
        if not isinstance(fv, FinalValidationArtifact):
            return self._fail(
                state, "FINAL_HEALTH_VALIDATION_MISSING", "缺少显式 validate_selected_menu 最终健康校验结果 (INV-007)"
            )
        if fv.status != "PASS":
            return self._fail(
                state, "FINAL_HEALTH_VALIDATION_FAILED", f"最终校验 verdict: {fv.status}"
            )
        matching_plans = [p for p in plans if getattr(p, "plan_id", "") == fv.plan_id]
        if not matching_plans:
            return self._fail(
                state,
                "FINAL_HEALTH_VALIDATION_PLAN_MISMATCH",
                f"最终校验 plan_id ({fv.plan_id}) 不在当前可行方案列表中",
            )
        best = matching_plans[0]

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
        self._trace_end(NodeType.MENU_DECISION.value)

        # 确定性回答 + ReviewArtifact(PASS)
        self._trace_start(NodeType.ANSWER_GENERATION.value)
        estimated_minutes = max(1, round(best.estimated_makespan_seconds / 60))
        answer = AuthoritativeAnswerBuilder.build(
            md,
            fv,
            state.build_id,
            time_note=f"按当前步骤估算，预计需要 {estimated_minutes} 分钟。",
        )
        if allow_polish and narrative_polish_enabled() and self._trace is not None:
            elapsed = time.perf_counter() - self._trace.processing_started_at
            if elapsed < 5.5:
                answer = NarrativePolisher().polish(
                    answer, timeout_seconds=2.0, trace=self._trace
                )
        self._trace_end(NodeType.ANSWER_GENERATION.value)
        rv = ReviewArtifact(
            artifact_id=uuid.uuid4(),
            request_id=UUID(request_id),
            status="PASS",
            content_hash="0" * 64,
        )

        state = reduce_workflow_state(
            state, action="set_artifact", artifact="menu_decision", value=md
        )
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="final_validation", value=fv
        )
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="answer", value=answer
        )
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="review", value=rv
        )
        state = reduce_workflow_state(
            state,
            action="record_receipts",
            receipts=self._as_authoritative_receipts(tool_ctx.tool_receipts),
        )
        state = reduce_workflow_state(state, action="unified_review", status="PASS")
        guard = self._guard_active(state, c4, session_id, lock_token, lost)
        if guard is not None:
            return guard
        state = reduce_workflow_state(state, action="atomic_commit")
        return state

    # ---- LangGraph 图节点实现 ----

    def _node_understand_intent(self, state: DietAgentState) -> dict[str, Any]:
        """节点 1：查询理解与上下文注入。"""
        request_id = state["request_id"]
        session_id = state["session_id"]
        message = state["message"]
        participant_refs = state["participant_refs"]
        user_id_mapping = state["user_id_mapping"]
        build_id = state["build_id"]
        c4 = state["c4"]
        wf_state = state["workflow_state"]
        tool_ctx = state["tool_context"]

        if state.get("lost") is not None:
            guard = self._guard_active(
                wf_state, c4, session_id, state["lock_token"], state["lost"],
            )
            if guard is not None:
                return {"workflow_state": guard, "is_terminal": True}

        if self._trace is None:
            self._trace = PerfTrace(request_id=request_id)

        wf_state = self._enter_node(wf_state, tool_ctx, NodeType.CONTEXT_BUILDING)
        self._trace_start(NodeType.CONTEXT_BUILDING.value)

        # 1. 注入检查
        _injection = detect_untrusted_instruction(message)
        if _injection:
            wf_state = self._fail(
                wf_state,
                "UNTRUSTED_INSTRUCTION_DETECTED",
                f"检测到指令注入: {_injection}",
            )
            return {"workflow_state": wf_state, "is_terminal": True}

        # 2. 上下文构建
        try:
            ctx, _manifest = c4.build_shared_context(
                session_id,
                participant_refs,
                {"raw_text": message, "timestamp": time.time()},
                user_id_mapping,
                request_id=request_id,
                build_id=build_id,
            )
        except PermanentConstraintLoadFailed as exc:
            wf_state = self._fail(
                wf_state, "PERMANENT_CONSTRAINT_LOAD_FAILED", str(exc)
            )
            return {"workflow_state": wf_state, "is_terminal": True}
        except ContextBudgetExceeded as exc:
            wf_state = self._fail(wf_state, "CONTEXT_BUDGET_EXCEEDED", str(exc))
            return {"workflow_state": wf_state, "is_terminal": True}

        wf_state = reduce_workflow_state(
            wf_state, action="set_context_ref", shared_context_ref=ctx.session_id
        )
        integrity = c4.validate_context_integrity(wf_state.shared_context_ref)
        if not integrity.get("valid", False):
            wf_state = self._fail(
                wf_state,
                "CONTEXT_INTEGRITY_FAILED",
                f"上下文完整性校验失败: {integrity.get('reason', 'core mismatch')}",
            )
            return {"workflow_state": wf_state, "is_terminal": True}

        d1_api.publish_analysis_event(
            request_id,
            "context_ready",
            f"已理解{len(participant_refs)}位参与者的需求",
            [],
        )
        d1_api.publish_answer_started(request_id)
        wf_state = reduce_workflow_state(
            wf_state, action="context_building", manifest_valid=True
        )
        self._trace_end(NodeType.CONTEXT_BUILDING.value)

        # R5: 优先关联有效待澄清事项
        lock_token = state["lock_token"]
        clar_state = None
        if hasattr(c4, "load_clarification_state"):
            try:
                clar_state = c4.load_clarification_state(session_id)
            except Exception as exc:
                wf_state = self._fail(
                    wf_state, "CLARIFICATION_STATE_LOOKUP_FAILED",
                    f"无法从数据库加载会话澄清状态: {exc}",
                    node=NodeType.QUERY_UNDERSTANDING,
                )
                return {
                    "workflow_state": wf_state,
                    "is_terminal": True,
                    "error_code": "CLARIFICATION_STATE_LOOKUP_FAILED",
                }

        is_v2 = bool(clar_state.get("protocol_version") == "v2") if clar_state is not None else False
        clar_rev = clar_state.get("clarification_revision") if clar_state else None

        active_pending = None
        if hasattr(c4, "load_active_clarification"):
            try:
                active_mysql = c4.load_active_clarification(session_id)
                if active_mysql:
                    pub = active_mysql.get("public_payload") or {}
                    priv = active_mysql.get("private_snapshot") or {}
                    active_pending = {
                        "question_id": active_mysql.get("question_id"),
                        "session_id": active_mysql.get("session_id"),
                        "status": active_mysql.get("status"),
                        "options": pub.get("options", []),
                        "question_text": pub.get("question_text", ""),
                        "query_plan_snapshot": priv.get("query_plan_snapshot"),
                        "option_modifications": priv.get("option_modifications", {}),
                        "current_menu_version": priv.get("current_menu_version"),
                        "expires_at": active_mysql.get("expires_at"),
                    }
            except Exception as exc:
                if is_v2:
                    wf_state = self._fail(
                        wf_state, "CLARIFICATION_LOOKUP_FAILED",
                        f"无法从数据库加载活跃澄清问题: {exc}",
                        node=NodeType.QUERY_UNDERSTANDING,
                    )
                    return {
                        "workflow_state": wf_state,
                        "is_terminal": True,
                        "error_code": "CLARIFICATION_LOOKUP_FAILED",
                    }
                logger.warning("load_active_clarification 失败: %s", exc)

        pending_items = []
        if hasattr(c4, "get_pending_clarifications"):
            pending_items = c4.get_pending_clarifications(session_id)

        for item in reversed(pending_items):
            if item.get("status") != "pending":
                continue
            question_id = item.get("question_id")
            if question_id and hasattr(c4, "is_clarification_committed"):
                try:
                    already_committed = c4.is_clarification_committed(session_id, question_id)
                except Exception as exc:
                    wf_state = self._fail(
                        wf_state, "CLARIFICATION_COMMIT_LOOKUP_FAILED",
                        f"无法验证澄清选项是否已提交: {exc}",
                        node=NodeType.QUERY_UNDERSTANDING,
                    )
                    return {
                        "workflow_state": wf_state,
                        "is_terminal": True,
                        "error_code": "CLARIFICATION_COMMIT_LOOKUP_FAILED",
                    }
                if already_committed:
                    if hasattr(c4, "consume_pending_clarification"):
                        try:
                            c4.consume_pending_clarification(
                                session_id, question_id=question_id, token=lock_token
                            )
                        except Exception:
                            logger.exception("清理已提交澄清问题的 Redis 残留失败: session_id=%s", session_id)
                    if active_pending is None and not is_v2:
                        _, selection_status = self._resolve_option_selection(
                            message, item.get("options", [])
                        )
                        if selection_status != "not_an_option":
                            wf_state = self._fail(
                                wf_state, "CLARIFICATION_ALREADY_APPLIED",
                                "该澄清选项已在之前的请求中提交，请提出新的用餐需求",
                                node=NodeType.QUERY_UNDERSTANDING,
                            )
                            return {
                                "workflow_state": wf_state,
                                "is_terminal": True,
                                "error_code": "CLARIFICATION_ALREADY_APPLIED",
                            }
                    continue
            if not is_v2 and active_pending is None:
                active_pending = item

        clar_resp = state.get("clarification_response")
        selected_option = None
        match_status = "not_an_option"
        pending_qid_to_consume = None
        selected_opt_id = None
        supersede_clarification = False
        clarification_transition = None

        if clar_resp:
            resp_qid = clar_resp.get("question_id")
            resp_opt_id = clar_resp.get("option_id")
            if not active_pending or active_pending.get("question_id") != resp_qid:
                if hasattr(c4, "is_clarification_committed") and c4.is_clarification_committed(session_id, resp_qid):
                    wf_state = self._fail(
                        wf_state, "CLARIFICATION_ALREADY_APPLIED",
                        "该澄清选项已在之前的请求中提交，请提出新的用餐需求",
                        node=NodeType.QUERY_UNDERSTANDING,
                    )
                    return {
                        "workflow_state": wf_state,
                        "is_terminal": True,
                        "error_code": "CLARIFICATION_ALREADY_APPLIED",
                    }
                wf_state = self._fail(
                    wf_state, "CLARIFICATION_STALE",
                    "所选澄清问题已失效或已被替代，请重新选择",
                    node=NodeType.QUERY_UNDERSTANDING,
                )
                return {
                    "workflow_state": wf_state,
                    "is_terminal": True,
                    "error_code": "CLARIFICATION_STALE",
                }
            if time.time() > active_pending.get("expires_at", float("inf")):
                wf_state = self._fail(
                    wf_state, "CLARIFICATION_EXPIRED",
                    "先前的调整选项已过期，请重新提出您的用餐需求",
                    node=NodeType.QUERY_UNDERSTANDING,
                )
                return {
                    "workflow_state": wf_state,
                    "is_terminal": True,
                    "error_code": "CLARIFICATION_EXPIRED",
                }
            options = active_pending.get("options", [])
            for opt in options:
                if int(opt.get("option_id", -1)) == int(resp_opt_id):
                    selected_option = opt
                    break
            if not selected_option:
                wf_state = self._fail(
                    wf_state, "OPTION_OUT_OF_RANGE",
                    f"您选择的选项超出范围，当前仅有 1 至 {len(options)} 个选项，请重新选择",
                    node=NodeType.QUERY_UNDERSTANDING,
                )
                return {
                    "workflow_state": wf_state,
                    "is_terminal": True,
                    "error_code": "OPTION_OUT_OF_RANGE",
                }
            match_status = "matched"
            pending_qid_to_consume = resp_qid
            selected_opt_id = int(resp_opt_id)

        elif active_pending is not None:
            # 1. 检查是否过期
            if time.time() > active_pending.get("expires_at", float("inf")):
                if hasattr(c4, "consume_pending_clarification"):
                    c4.consume_pending_clarification(
                        session_id, question_id=active_pending.get("question_id"), token=lock_token
                    )
                sel, status = self._resolve_option_selection(
                    message, active_pending.get("options", [])
                )
                if status in ("matched", "ambiguous", "out_of_range"):
                    return {
                        "workflow_state": wf_state,
                        "inquiry_needed": True,
                        "diagnosis_code": "CLARIFICATION_EXPIRED",
                        "inquiry_reason": "先前的调整选项已过期，请重新提出您的用餐需求",
                        "inquiry_options": ["请重新提出您的用餐需求"],
                        "structured_options": [
                            {"option_id": 1, "text": "请重新提出您的用餐需求", "modifications": {}}
                        ],
                    }
            else:
                options = active_pending.get("options", [])
                sel_opt, m_status = self._resolve_option_selection(message, options)
                if (
                    is_v2
                    and m_status in ("matched", "ambiguous", "out_of_range")
                ):
                    wf_state = self._fail(
                        wf_state, "CLARIFICATION_REFERENCE_REQUIRED",
                        "请使用选项按钮或携带澄清问题标识回复，无法直接绑定无标识的选项回复",
                        node=NodeType.QUERY_UNDERSTANDING,
                    )
                    return {
                        "workflow_state": wf_state,
                        "is_terminal": True,
                        "error_code": "CLARIFICATION_REFERENCE_REQUIRED",
                    }
                selected_option = sel_opt
                match_status = m_status
                if match_status == "out_of_range":
                    return {
                        "workflow_state": wf_state,
                        "inquiry_needed": True,
                        "diagnosis_code": "OPTION_OUT_OF_RANGE",
                        "inquiry_reason": f"您选择的选项超出范围，当前仅有 1 至 {len(options)} 个选项，请重新选择",
                        "inquiry_options": [opt.get("text", "") for opt in options],
                        "structured_options": options,
                    }
                elif match_status == "ambiguous":
                    return {
                        "workflow_state": wf_state,
                        "inquiry_needed": True,
                        "diagnosis_code": "AMBIGUOUS_OPTION_SELECTION",
                        "inquiry_reason": "未能确定您选择的选项，请明确回复选项编号（如'选项1'或'选第二个'）",
                        "inquiry_options": [opt.get("text", "") for opt in options],
                        "structured_options": options,
                    }
                elif match_status == "matched" and selected_option:
                    pending_qid_to_consume = active_pending.get("question_id")
                    selected_opt_id = int(selected_option.get("option_id", 1))
                elif match_status == "not_an_option":
                    # 用户提出了全新需求，不再基于旧提议调整；标记替代旧问题（事务提交时生效）
                    supersede_clarification = True

        if match_status == "matched" and selected_option:
            from food_agent_v2.contracts.clarification import ClarificationTransition
            clarification_transition = ClarificationTransition(
                expected_question_id=pending_qid_to_consume,
                selected_option_id=selected_opt_id,
                expected_revision=clar_rev if is_v2 else None,
            )
            qp_snapshot = active_pending.get("query_plan_snapshot")
            base_qp = None
            if qp_snapshot and isinstance(qp_snapshot, dict):
                try:
                    base_qp = QueryPlanArtifact(**qp_snapshot)
                except Exception:
                    base_qp = None
            if base_qp is None:
                base_qp = self._build_query_plan(
                    IntentDelta(
                        intent="new_recommendation",
                        meal_types=("dinner",),
                    ),
                    request_id,
                    participant_refs,
                )

            opt_mods_map = active_pending.get("option_modifications") or {}
            if is_v2:
                mods = opt_mods_map.get(selected_opt_id) or opt_mods_map.get(str(selected_opt_id)) or {}
            else:
                mods = selected_option.get("modifications") or opt_mods_map.get(selected_opt_id) or opt_mods_map.get(str(selected_opt_id)) or {}
            updates: dict[str, Any] = {"request_id": UUID(request_id)}
            if "dish_count_requested" in mods:
                updates["dish_count_requested"] = mods["dish_count_requested"]
            elif "dish_count" in mods:
                updates["dish_count_requested"] = mods["dish_count"]
            if "time_constraint_seconds" in mods:
                updates["time_constraint_seconds"] = mods["time_constraint_seconds"]
            if "time_constraint_policy" in mods:
                updates["time_constraint_policy"] = mods["time_constraint_policy"]
            if "dish_types" in mods:
                updates["dish_types"] = tuple(mods["dish_types"])
            if "meal_types" in mods:
                updates["meal_types"] = tuple(mods["meal_types"])
            elif "meal_type" in mods:
                updates["meal_types"] = (mods["meal_type"],)
            if "taste_tags" in mods:
                updates["taste_tags"] = tuple(mods["taste_tags"])

            qp = base_qp.model_copy(update=updates)
            qp = qp.model_copy(update={"content_hash": self._content_hash(qp)})
            tool_ctx.previous_results["query_plan"] = qp
            if qp.time_constraint_policy == "hard" and qp.time_constraint_seconds:
                tool_ctx.max_estimated_time_seconds = qp.time_constraint_seconds
            elif qp.time_constraint_policy == "soft":
                tool_ctx.max_estimated_time_seconds = None

            wf_state = reduce_workflow_state(
                wf_state, action="set_artifact", artifact="query_plan", value=qp
            )

            d1_api.publish_analysis_event(
                request_id,
                "query_understanding",
                f"已确认调整方案：{selected_option.get('text', '')}",
                [],
            )
            self._trace_end(NodeType.QUERY_UNDERSTANDING.value)

            return {
                "intent": IntentDelta(
                    intent="new_recommendation",
                    query=selected_option.get("text", message),
                    meal_types=qp.meal_types,
                ),
                "query_plan": qp,
                "workflow_state": wf_state,
                "tool_context": tool_ctx,
                "locked_recipe_ids": [],
                "rejected_recipe_ids": [],
                "pending_clarification_to_consume": pending_qid_to_consume,
                "selected_option_id": selected_opt_id,
                "supersede_clarification": False,
                "clarification_transition": clarification_transition,
                "clarification_revision": clar_rev,
                "is_v2": is_v2,
            }

        elif supersede_clarification:
            from food_agent_v2.contracts.clarification import ClarificationTransition
            clarification_transition = ClarificationTransition(
                supersede_current=True,
                expected_revision=clar_rev if is_v2 else None,
            )

        # 3. 意图解析
        intent = FastIntentRouter.route(message, tuple(participant_refs))
        if intent.intent in ("needs_clarification", "conflict"):
            reason = intent.clarification_reason or "需求需要进一步澄清"
            return {
                "intent": intent,
                "workflow_state": wf_state,
                "inquiry_needed": True,
                "diagnosis_code": "NEEDS_CLARIFICATION",
                "inquiry_reason": reason,
                "inquiry_options": [
                    "请明确就餐人数或具体餐次（早餐/午餐/晚餐）",
                    "请说明是否有特殊的健康忌口或饮食偏好",
                ],
                "supersede_clarification": supersede_clarification,
                "clarification_transition": clarification_transition,
                "clarification_revision": clar_rev,
                "is_v2": is_v2,
            }

        # R3 & R4: 处理多轮 replace 与 restore
        if intent.intent in ("replace", "restore"):
            session_state = c4.get_session_state(session_id) or {}
            current_menu = session_state.get("current_menu") or {}
            if not current_menu or not current_menu.get("recipe_ids"):
                return {
                    "intent": intent,
                    "workflow_state": wf_state,
                    "inquiry_needed": True,
                    "diagnosis_code": "NO_CURRENT_MENU",
                    "inquiry_reason": "当前没有可操作的菜单",
                    "inquiry_options": [
                        "请先提出您的用餐需求，生成推荐菜单后再进行调整",
                    ],
                }

            if intent.intent == "restore":
                previous_menu = self._previous_menu_version(session_state)
                if previous_menu is None:
                    return {
                        "intent": intent,
                        "workflow_state": wf_state,
                        "inquiry_needed": True,
                        "diagnosis_code": "NO_PREVIOUS_MENU",
                        "inquiry_reason": "没有可恢复的上一版菜单",
                        "inquiry_options": [
                            "当前会话仅有一版菜单或无更早历史，无法恢复上一版",
                        ],
                    }

                recipe_ids = [int(value) for value in previous_menu.get("recipe_ids", [])]
                snapshot = previous_menu.get("query_plan") or session_state.get("query_plan")
                intent = self._restore_intent(snapshot, message, len(recipe_ids))

                self._trace_start(NodeType.QUERY_UNDERSTANDING.value)
                qp = self._build_query_plan(intent, request_id, participant_refs)
                wf_state = reduce_workflow_state(
                    wf_state, action="set_artifact", artifact="query_plan", value=qp
                )
                tool_ctx.previous_results["query_plan"] = qp
                if getattr(qp, "health_exclusions", ()):
                    wf_state = self._handle_query_plan_exclusions(
                        wf_state, qp, session_id, c4, user_id_mapping
                    )
                    if wf_state.is_terminal():
                        return {"workflow_state": wf_state, "is_terminal": True}
                if qp.time_constraint_policy == "hard" and qp.time_constraint_seconds:
                    tool_ctx.max_estimated_time_seconds = qp.time_constraint_seconds

                from food_agent_v2.b3.recipe_views import get_view_builder

                builder = get_view_builder()
                if (
                    not recipe_ids
                    or len(set(recipe_ids)) != len(recipe_ids)
                    or any(builder.build_retrieval_view(rid) is None for rid in recipe_ids)
                ):
                    wf_state = self._fail(
                        wf_state,
                        "RESTORE_VERSION_UNAVAILABLE",
                        "上一版菜单在当前构建中不可用",
                        node=NodeType.QUERY_UNDERSTANDING,
                    )
                    return {
                        "workflow_state": wf_state,
                        "is_terminal": True,
                        "error_code": "RESTORE_VERSION_UNAVAILABLE",
                        "error_message": "上一版菜单在当前构建中不可用",
                    }

                d1_api.publish_analysis_event(
                    request_id, "query_understanding", "理解需求完成（恢复历史菜单）", []
                )
                self._trace_end(NodeType.QUERY_UNDERSTANDING.value)

                # 恢复历史菜单：候选集即历史菜品，不需要 RAG 检索（检索次数为 0），全量锁定
                execution_context = dict(state.get("execution_context") or {})
                execution_context.setdefault("known_candidates", set()).update(recipe_ids)
                execution_context.setdefault("known_evidence", set()).add(f"history:{qp.input_fingerprint}")
                return {
                    "intent": intent,
                    "query_plan": qp,
                    "workflow_state": wf_state,
                    "tool_context": tool_ctx,
                    "candidate_recipes": list(recipe_ids),
                    "locked_recipe_ids": list(recipe_ids),
                    "rejected_recipe_ids": [],
                    "execution_context": execution_context,
                }

            # replace 意图：解析替换目标
            target_recipe_id = self._resolve_replace_target(message, current_menu)
            if target_recipe_id is None:
                current_items = current_menu.get("items", [])
                item_names = [str(it.get("name")) for it in current_items if it.get("name")]
                menu_hint = f"（当前菜单包含：{'、'.join(item_names)}）" if item_names else ""
                return {
                    "intent": intent,
                    "workflow_state": wf_state,
                    "inquiry_needed": True,
                    "diagnosis_code": "AMBIGUOUS_REPLACE_TARGET",
                    "inquiry_reason": "请明确要替换的当前菜名",
                    "inquiry_options": [
                        f"请明确说明您想替换哪道菜{menu_hint}",
                    ],
                }

            # 继承上一轮硬约束
            previous_plan_dict = self._replace_previous_plan(session_state.get("query_plan"))
            rewrite = QueryNormalizer(self._llm).normalize(
                message,
                tuple(participant_refs),
                previous_query_plan=previous_plan_dict,
            )
            intent = self._apply_semantic_rewrite(
                replace(intent, target_recipe_id=target_recipe_id),
                rewrite,
                tuple(participant_refs),
                has_current_menu=True,
            )

            self._trace_start(NodeType.QUERY_UNDERSTANDING.value)
            qp = self._build_query_plan(intent, request_id, participant_refs)
            # 替换保持原菜单菜数
            current_ids = list(current_menu.get("recipe_ids", []))
            qp = qp.model_copy(update={"dish_count_requested": len(current_ids)})
            wf_state = reduce_workflow_state(
                wf_state, action="set_artifact", artifact="query_plan", value=qp
            )
            tool_ctx.previous_results["query_plan"] = qp
            if getattr(qp, "health_exclusions", ()):
                wf_state = self._handle_query_plan_exclusions(
                    wf_state, qp, session_id, c4, user_id_mapping
                )
                if wf_state.is_terminal():
                    return {"workflow_state": wf_state, "is_terminal": True}
            if qp.time_constraint_policy == "hard" and qp.time_constraint_seconds:
                tool_ctx.max_estimated_time_seconds = qp.time_constraint_seconds

            d1_api.publish_analysis_event(
                request_id, "query_understanding", "理解需求完成（替换指定菜品）", []
            )
            self._trace_end(NodeType.QUERY_UNDERSTANDING.value)

            return {
                "intent": intent,
                "query_plan": qp,
                "workflow_state": wf_state,
                "tool_context": tool_ctx,
                "locked_recipe_ids": [],
                "rejected_recipe_ids": [],
            }

        # 4. 多轮与改写 (add_constraint / reject_plan / new_recommendation)
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

        # 5. 构建 QueryPlanArtifact
        self._trace_start(NodeType.QUERY_UNDERSTANDING.value)
        qp = self._build_query_plan(intent, request_id, participant_refs)
        wf_state = reduce_workflow_state(
            wf_state, action="set_artifact", artifact="query_plan", value=qp
        )
        tool_ctx.previous_results["query_plan"] = qp

        if getattr(qp, "health_exclusions", ()):
            wf_state = self._handle_query_plan_exclusions(
                wf_state, qp, session_id, c4, user_id_mapping
            )
            if wf_state.is_terminal():
                return {"workflow_state": wf_state, "is_terminal": True}

        if qp.time_constraint_policy == "hard" and qp.time_constraint_seconds:
            tool_ctx.max_estimated_time_seconds = qp.time_constraint_seconds

        d1_api.publish_analysis_event(
            request_id, "query_understanding", "理解需求完成", []
        )
        self._trace_end(NodeType.QUERY_UNDERSTANDING.value)

        # 多轮增量支持：如果是在已有菜单上追加约束或否定
        locked_ids: list[int] = []
        rejected_ids: list[int] = []
        if has_current_menu and intent.intent in ("add_constraint", "reject_plan"):
            current = (c4.get_session_state(session_id) or {}).get("current_menu") or {}
            current_ids = list(current.get("recipe_ids", []))
            if current_ids:
                qp = qp.model_copy(update={"dish_count_requested": len(current_ids)})
                tool_ctx.previous_results["query_plan"] = qp
                wf_state = reduce_workflow_state(
                    wf_state, action="set_artifact", artifact="query_plan", value=qp
                )

        execution_context = dict(state.get("execution_context") or {})
        return {
            "intent": intent,
            "query_plan": qp,
            "workflow_state": wf_state,
            "tool_context": tool_ctx,
            "locked_recipe_ids": locked_ids,
            "rejected_recipe_ids": rejected_ids,
            "execution_context": execution_context,
            "supersede_clarification": supersede_clarification,
            "clarification_transition": clarification_transition,
            "clarification_revision": clar_rev,
            "is_v2": is_v2,
        }

    def _node_search_candidates(self, state: DietAgentState) -> dict[str, Any]:
        """节点 2：C1 混合检索工具调用。"""
        wf_state = state["workflow_state"]
        c4 = state["c4"]
        session_id = state["session_id"]
        lock_token = state["lock_token"]
        lost = state["lost"]
        tool_ctx = state["tool_context"]
        intent = state["intent"]
        request_id = state["request_id"]
        build_id = state["build_id"]

        guard = self._guard_active(wf_state, c4, session_id, lock_token, lost)
        if guard is not None:
            return {"workflow_state": guard, "is_terminal": True}

        self._trace_start("retrieval")
        tool_ctx.node_id = self._NODE_RETRIEVE
        handler = ToolHandler(tool_ctx)

        res = search_candidates(
            query=self._retrieval_query(intent),
            top_k=40,
            query_plan=state.get("query_plan"),
            participant_user_mapping=state.get("user_id_mapping"),
            build_id=build_id,
        )
        self._trace_end("retrieval")

        # R2: 技术错误 fail-closed，记录真实失败回执，进入错误终态，绝不转为用户追问
        if res.status == "error":
            handler._emit_receipt(
                "retrieve_recipes",
                {"query": self._retrieval_query(intent), "top_k": 40},
                {"error": res.message},
                False,
                res.error_code,
            )
            wf_state = self._fail(
                wf_state,
                res.error_code or "RETRIEVAL_FAILED",
                f"检索服务调用失败: {res.message}",
                node=NodeType.QUERY_UNDERSTANDING,
            )
            return {
                "workflow_state": wf_state,
                "is_terminal": True,
                "error_code": res.error_code,
                "error_message": res.message,
            }

        candidate_ids = res.data.get("candidates", []) if res.data else []
        retrieval = res.data.get("retrieval_result") if res.data else None
        if retrieval is not None:
            tool_ctx.previous_results["retrieval"] = retrieval

        handler._emit_receipt(
            "retrieve_recipes",
            {"query": self._retrieval_query(intent), "top_k": 40},
            {"total": len(candidate_ids), "candidates": [{"recipe_id": rid} for rid in candidate_ids]},
            True,
            None,
        )

        # 多轮追加/替换场景下，合并当前菜单菜品，保证一起接受健康审查
        current_menu = (c4.get_session_state(session_id) or {}).get("current_menu") or {}
        current_ids = list(current_menu.get("recipe_ids", []))
        if current_ids and intent and intent.intent in ("add_constraint", "reject_plan", "replace"):
            candidate_ids = list(dict.fromkeys(current_ids + candidate_ids))

        if not candidate_ids:
            return {
                "candidate_recipes": [],
                "inquiry_needed": True,
                "diagnosis_code": "CANDIDATES_REQUIRED",
                "inquiry_reason": "未能检索到符合您当前偏好或限制的菜品候选。",
                "inquiry_options": [
                    "选项 1: 放宽菜系或口味要求，以检索更多菜品",
                    "选项 2: 调整特定的食材限制或特殊要求",
                    "选项 3: 更换搜索关键词后重新推荐",
                ],
            }

        d1_api.publish_analysis_event(
            request_id,
            "retrieval",
            f"检索完成，找到 {len(candidate_ids)} 道候选菜品",
            [],
        )
        return {
            "candidate_recipes": candidate_ids,
            "retrieval_result": retrieval,
            "tool_context": tool_ctx,
        }

    def _node_audit_health(self, state: DietAgentState) -> dict[str, Any]:
        """节点 3：B4 健康审查工具调用。"""
        wf_state = state["workflow_state"]
        c4 = state["c4"]
        session_id = state["session_id"]
        lock_token = state["lock_token"]
        lost = state["lost"]
        tool_ctx = state["tool_context"]
        candidate_ids = state["candidate_recipes"]
        request_id = state["request_id"]
        intent = state.get("intent")
        build_id = state.get("build_id", "")

        guard = self._guard_active(wf_state, c4, session_id, lock_token, lost)
        if guard is not None:
            return {"workflow_state": guard, "is_terminal": True}

        self._trace_start(NodeType.HEALTH_MENU_PLANNING.value)
        tool_ctx.node_id = self._NODE_HEALTH
        handler = ToolHandler(tool_ctx)

        res = audit_recipe_health(
            candidate_recipe_ids=candidate_ids,
            participant_user_mapping=state.get("user_id_mapping", {}),
            build_id=build_id,
            context_service=c4,
            session_id=session_id,
            request_id=request_id,
        )
        self._trace_end(NodeType.HEALTH_MENU_PLANNING.value)

        # R2: 严格区分技术错误与业务无解。技术错误 fail-closed，记录真实失败回执，绝不转用户追问
        if res.status == "error":
            handler._emit_receipt(
                "evaluate_recipe_health",
                {"recipe_ids": candidate_ids},
                {"error": res.message},
                False,
                res.error_code,
            )
            wf_state = self._fail(
                wf_state,
                res.error_code or "HEALTH_AUDIT_ERROR",
                f"健康审查技术故障: {res.message}",
                node=NodeType.HEALTH_MENU_PLANNING,
            )
            return {
                "workflow_state": wf_state,
                "is_terminal": True,
                "error_code": res.error_code,
                "error_message": res.message,
            }

        batch = res.data.get("batch") if res.data else None
        if batch is not None:
            tool_ctx.previous_results["health_evaluation"] = batch
        safe_ids = list(res.data.get("safe_recipe_ids", []) if res.data else [])
        excluded_ids = list(res.data.get("excluded_recipe_ids", []) if res.data else [])
        excluded_details = res.data.get("excluded_details", {}) if res.data else {}

        # 正常审查完成（包括业务 0 安全菜），回执记录客观审查事实
        handler._emit_receipt(
            "evaluate_recipe_health",
            {"recipe_ids": candidate_ids},
            {"safe_recipe_ids": safe_ids, "excluded_recipe_ids": excluded_ids},
            True,
            None,
        )

        if not safe_ids:
            d1_api.publish_analysis_event(
                request_id,
                "health_evaluation",
                "健康审查完成（所有候选菜品均不符合健康安全要求）",
                [],
            )
            return {
                "safe_recipe_ids": [],
                "excluded_recipe_ids": excluded_ids,
                "health_evaluation": batch,
                "health_excluded_details": excluded_details,
                "inquiry_needed": True,
                "diagnosis_code": "NO_SAFE_CANDIDATE",
                "inquiry_reason": (
                    "经过严格的健康安全审核，本次检索出的所有候选菜品均包含您或家人的健康忌口/过敏成分，"
                    "没有找到完全安全的菜品。"
                ),
                "inquiry_options": [
                    "选项 1: 放宽非健康类口味或菜系偏好，重新进行广度搜索",
                    "选项 2: 指定希望包含的安全食材（如蔬菜、豆腐等）重新推荐",
                ],
            }

        d1_api.publish_analysis_event(
            request_id, "health_evaluation", "健康审查完成", []
        )

        # 多轮 delta 规划计算（锁定当前安全菜，排除被否定/替换菜；恢复历史菜单）
        locked_ids = list(state.get("locked_recipe_ids") or [])
        rejected_ids = list(state.get("rejected_recipe_ids") or [])

        if intent and intent.intent == "restore":
            candidate_ids = state.get("candidate_recipes", [])
            if set(safe_ids) != set(candidate_ids):
                d1_api.publish_analysis_event(
                    request_id,
                    "health_evaluation",
                    "健康审查完成（历史菜单菜品不符合当前健康要求）",
                    [],
                )
                wf_state = reduce_workflow_state(
                    wf_state, action="health_menu_planning", result="no_safe_menu"
                )
                return {
                    "workflow_state": wf_state,
                    "safe_recipe_ids": safe_ids,
                    "excluded_recipe_ids": excluded_ids,
                    "health_evaluation": batch,
                    "health_excluded_details": excluded_details,
                    "is_terminal": True,
                }
            return {
                "safe_recipe_ids": safe_ids,
                "excluded_recipe_ids": excluded_ids,
                "health_evaluation": batch,
                "health_excluded_details": excluded_details,
                "locked_recipe_ids": list(candidate_ids),
                "rejected_recipe_ids": [],
                "tool_context": tool_ctx,
            }

        if intent and intent.intent == "replace":
            current_menu = (c4.get_session_state(session_id) or {}).get("current_menu") or {}
            current_ids = list(current_menu.get("recipe_ids", []))
            target_id = intent.target_recipe_id
            expected_locked = [rid for rid in current_ids if rid != target_id]
            unsafe_locked = [rid for rid in expected_locked if rid not in safe_ids]
            if unsafe_locked:
                unsafe_names = [
                    str(item.get("name"))
                    for item in current_menu.get("items", [])
                    if item.get("recipe_id") in unsafe_locked
                ]
                names_str = "、".join(unsafe_names) if unsafe_names else str(unsafe_locked)
                d1_api.publish_analysis_event(
                    request_id,
                    "health_evaluation",
                    f"健康审查完成（原菜单保留菜品 {names_str} 不符合当前健康要求）",
                    [],
                )
                return {
                    "safe_recipe_ids": safe_ids,
                    "excluded_recipe_ids": excluded_ids,
                    "inquiry_needed": True,
                    "diagnosis_code": "LOCKED_RECIPE_UNSAFE",
                    "inquiry_reason": f"原菜单中需要保留的菜品（{names_str}）不再符合当前健康安全要求，无法仅替换指定菜品。",
                    "inquiry_options": [
                        "选项 1: 重新推荐一套满足最新健康要求的完整菜单",
                        "选项 2: 调整导致该菜品不安全的健康偏好后重新替换",
                    ],
                }
            delta = DeltaPlanner().plan(
                current_ids,
                IntentDelta(
                    intent=intent.intent,
                    target_recipe_id=intent.target_recipe_id,
                ),
                safe_ids,
            )
            locked_ids = list(delta.locked_recipe_ids)
            rejected_ids = list(delta.rejected_recipe_ids)
        elif intent and intent.intent in ("add_constraint", "reject_plan"):
            current_menu = (c4.get_session_state(session_id) or {}).get("current_menu") or {}
            current_ids = list(current_menu.get("recipe_ids", []))
            if current_ids:
                delta = DeltaPlanner().plan(
                    current_ids,
                    IntentDelta(
                        intent=intent.intent,
                        target_recipe_id=intent.target_recipe_id,
                    ),
                    safe_ids,
                )
                locked_ids = list(delta.locked_recipe_ids)
                rejected_ids = list(delta.rejected_recipe_ids)

        return {
            "safe_recipe_ids": safe_ids,
            "excluded_recipe_ids": excluded_ids,
            "health_evaluation": batch,
            "health_excluded_details": excluded_details,
            "locked_recipe_ids": locked_ids,
            "rejected_recipe_ids": rejected_ids,
            "tool_context": tool_ctx,
        }

    def _node_combine_menu(self, state: DietAgentState) -> dict[str, Any]:
        """节点 4：C2 营养菜单规划工具调用与瓶颈精确诊断（零静默放宽）。"""
        wf_state = state["workflow_state"]
        c4 = state["c4"]
        session_id = state["session_id"]
        lock_token = state["lock_token"]
        lost = state["lost"]
        tool_ctx = state["tool_context"]
        qp = state["query_plan"]
        request_id = state["request_id"]
        safe_ids = state["safe_recipe_ids"]
        locked = state.get("locked_recipe_ids") or []
        rejected = state.get("rejected_recipe_ids") or []

        guard = self._guard_active(wf_state, c4, session_id, lock_token, lost)
        if guard is not None:
            return {"workflow_state": guard, "is_terminal": True}

        # 默认菜数复用 MenuHardConstraints 默认值 (5)，删除独立硬编码 4
        dish_count = qp.dish_count_requested or MenuHardConstraints().dish_count
        time_limit = (
            qp.time_constraint_seconds
            if qp.time_constraint_policy == "hard"
            else None
        )

        tool_ctx.node_id = self._NODE_HEALTH
        res = combine_nutritional_menu(
            safe_recipe_ids=safe_ids,
            dish_count=dish_count,
            nutrition_goal_codes=getattr(qp, "nutrition_goal_codes", ()),
            time_limit_seconds=time_limit,
            locked_recipe_ids=set(locked),
            rejected_recipe_ids=set(rejected),
            retrieval_candidates=getattr(state.get("retrieval_result"), "candidates", []),
            dish_types=getattr(qp, "dish_types", ()),
            tool_context=tool_ctx,
        )

        if res.status == "error":
            wf_state = self._fail(
                wf_state,
                res.error_code or "PLANNING_ERROR",
                f"菜单规划技术故障: {res.message}",
                node=NodeType.HEALTH_MENU_PLANNING,
            )
            return {
                "workflow_state": wf_state,
                "is_terminal": True,
                "error_code": res.error_code,
                "error_message": res.message,
            }

        if not res.success:
            code = res.error_code or "NO_FEASIBLE_MENU"
            diag = res.diagnostics or {}

            if code == "TIME_LIMIT_EXCEEDED":
                min_min = diag.get("min_needed_minutes", 30)
                req_min = diag.get("requested_time_minutes", 20)
                req_cnt = diag.get("requested_dish_count", dish_count)
                reason = (
                    f"您要求在 {req_min} 分钟内完成 {req_cnt} 道菜。在满足健康安全条件的前提下，"
                    f"基于当前安全候选菜谱，最快制作组合预计需要约 {min_min} 分钟，"
                    f"因此无法在不超时的情况下凑齐 {req_cnt} 道菜。"
                )
                structured_options = [
                    {
                        "option_id": 1,
                        "text": f"选项 1: 将时间限制放宽至 {min_min} 分钟或以上，保留 {req_cnt} 道菜",
                        "modifications": {
                            "time_constraint_seconds": min_min * 60,
                            "time_constraint_policy": "hard",
                        },
                    },
                    {
                        "option_id": 2,
                        "text": f"选项 2: 调整菜品数量为 {max(1, req_cnt - 1)} 道菜，以缩短烹饪制作耗时",
                        "modifications": {
                            "dish_count_requested": max(1, req_cnt - 1),
                        },
                    },
                    {
                        "option_id": 3,
                        "text": "选项 3: 取消严格时间限制，优先保证营养均衡与菜品丰富度",
                        "modifications": {
                            "time_constraint_policy": "soft",
                            "time_constraint_seconds": None,
                        },
                    },
                ]
            elif code == "SAFE_CANDIDATE_SHORTAGE":
                safe_cnt = diag.get("safe_count", len(safe_ids))
                req_cnt = diag.get("requested_count", dish_count)
                reason = (
                    f"您要求推荐 {req_cnt} 道菜。经过严格的健康安全审核（已为您排除相关的过敏及禁忌食材），"
                    f"符合条件的健康安全菜谱仅剩 {safe_cnt} 道，不足以组合成 {req_cnt} 道菜的完整方案。"
                )
                structured_options = [
                    {
                        "option_id": 1,
                        "text": f"选项 1: 接受推荐当前的 {safe_cnt} 道安全菜品",
                        "modifications": {
                            "dish_count_requested": safe_cnt,
                        },
                    },
                    {
                        "option_id": 2,
                        "text": "选项 2: 放宽非硬性偏好（如特定菜系或口味要求），以检索更多安全候选",
                        "modifications": {
                            "expand_search": True,
                        },
                    },
                    {
                        "option_id": 3,
                        "text": "选项 3: 补充其他想吃的食材或餐次需求后重新检索",
                        "modifications": {
                            "re_search": True,
                        },
                    },
                ]
            else:
                reason = "在满足健康要求的前提下，现有安全菜品无法同时满足您的所有结构性搭配需求（如特定汤品或主食）。"
                structured_options = [
                    {
                        "option_id": 1,
                        "text": "选项 1: 调整部分特定品类搭配要求（如允许不含特定汤品/主食）",
                        "modifications": {
                            "dish_types": [],
                        },
                    },
                    {
                        "option_id": 2,
                        "text": "选项 2: 扩大搜索范围以检索更多符合品类要求的候选菜",
                        "modifications": {
                            "expand_search": True,
                        },
                    },
                ]

            d1_api.publish_analysis_event(
                request_id,
                "menu_planning",
                f"菜单规划受阻: {reason}",
                [],
            )
            return {
                "inquiry_needed": True,
                "diagnosis_code": code,
                "inquiry_reason": reason,
                "inquiry_options": [opt["text"] for opt in structured_options],
                "structured_options": structured_options,
                "diagnostics": diag,
            }

        plans = res.data.get("plans", []) if res.data else []
        intent = state.get("intent")
        if intent and intent.intent == "restore":
            recipe_ids = state.get("candidate_recipes", [])
            expected = set(recipe_ids)
            plans = [
                plan
                for plan in plans
                if set(plan.recipe_ids) == expected and len(plan.recipe_ids) == len(recipe_ids)
            ]
            if not plans:
                wf_state = reduce_workflow_state(
                    wf_state, action="health_menu_planning", result="no_feasible_menu"
                )
                return {"workflow_state": wf_state, "is_terminal": True}
            for plan in plans:
                plan.recipe_ids = list(recipe_ids)

        tool_ctx.previous_results["feasible_menus"] = plans
        tool_ctx.safe_recipe_ids = list(safe_ids)

        d1_api.publish_analysis_event(
            request_id,
            "menu_planning",
            f"菜单方案生成完成，生成 {len(plans)} 个可行方案",
            [],
        )
        return {
            "feasible_menus": plans,
            "tool_context": tool_ctx,
        }

    def _node_inquire_user(self, state: DietAgentState) -> dict[str, Any]:
        """节点 5：协商追问（Human-in-the-Loop，零静默放宽）。"""
        request_id = state["request_id"]
        inquiry_reason = state.get("inquiry_reason")
        options = state.get("inquiry_options", [])
        structured_options = state.get("structured_options")
        code = state.get("diagnosis_code") or "NEEDS_CLARIFICATION"
        c4 = state.get("c4")
        session_id = state.get("session_id")
        qp = state.get("query_plan")

        action = state.get("current_action")
        if action and action.action == ActionType.ASK_USER:
            args = action.arguments or {}
            if "reason" in args and (not inquiry_reason or inquiry_reason == ""):
                inquiry_reason = args["reason"]
            if "options" in args and not structured_options:
                raw_opts = args["options"]
                if raw_opts and isinstance(raw_opts[0], dict):
                    structured_options = raw_opts
                elif raw_opts:
                    structured_options = [
                        {"option_id": idx + 1, "text": str(o), "modifications": {}}
                        for idx, o in enumerate(raw_opts)
                    ]
            if "inquiry_category" in args and (not code or code == "NEEDS_CLARIFICATION"):
                code = args["inquiry_category"]

        if not inquiry_reason:
            inquiry_reason = "需要澄清您的需求"

        if not structured_options:
            structured_options = [
                {"option_id": idx + 1, "text": opt, "modifications": {}}
                for idx, opt in enumerate(options)
            ]
        if not structured_options or len(structured_options) < 2:
            base_opts = structured_options or []
            if len(base_opts) == 0:
                structured_options = [
                    {"option_id": 1, "text": "调整需求后重新规划", "modifications": {}},
                    {"option_id": 2, "text": "放宽时间偏好重新推荐", "modifications": {"time_constraint_policy": "relax"}},
                ]
            elif len(base_opts) == 1:
                structured_options = [
                    base_opts[0],
                    {"option_id": 2, "text": "放宽时间偏好重新推荐", "modifications": {"time_constraint_policy": "relax"}},
                ]
        structured_options = structured_options[:3]

        lines = [inquiry_reason, "", "您可以考虑以下调整方案："]
        for opt in structured_options:
            lines.append(f"- {opt['text']}")
        inquiry_text = "\n".join(lines)

        # R5: 确定性派生 question_id，暂存状态转移声明（不提前推送到 Redis/SSE，等待 commit 成功提交）
        question_id = f"q_{request_id}"
        qp_snapshot = qp.model_dump(mode="json") if (qp and hasattr(qp, "model_dump")) else None
        current_menu = (c4.get_session_state(session_id) or {}).get("current_menu") or {} if (c4 and session_id) else {}

        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        pub_payload = ClarificationPublicPayload(
            question_text=inquiry_text,
            options=[
                ClarificationOptionView(
                    option_id=int(opt["option_id"]),
                    text=str(opt["text"]),
                )
                for opt in structured_options
            ],
            inquiry_category=code,
        )

        option_modifications = {
            int(opt["option_id"]): dict(opt.get("modifications") or {})
            for opt in structured_options
        }

        priv_snapshot = ClarificationPrivateSnapshot(
            query_plan_snapshot=qp_snapshot,
            option_modifications=option_modifications,
            current_menu_version=current_menu.get("plan_id"),
            evidence_refs=[],
        )

        clar_rev = state.get("clarification_revision")
        is_v2 = bool(state.get("is_v2"))
        transition = ClarificationTransition(
            expected_question_id=state.get("pending_clarification_to_consume"),
            selected_option_id=state.get("selected_option_id"),
            expected_revision=clar_rev if is_v2 else None,
            next_question_id=question_id,
            next_public_payload=pub_payload.model_dump(),
            next_private_snapshot=priv_snapshot.model_dump(),
            next_expires_at=time.time() + 3600,
            supersede_current=state.get("supersede_clarification", False),
        )

        wf_state = state["workflow_state"]
        wf_state = reduce_workflow_state(
            wf_state, action="query_understanding", needs_clarification=True
        )
        wf_state.error = WorkflowError(code, inquiry_text)

        d1_api.publish_analysis_event(
            request_id, "constraint_inquiry", inquiry_reason, []
        )

        return {
            "workflow_state": wf_state,
            "is_terminal": True,
            "clarification_transition": transition,
        }

    def _node_validate_and_answer(self, state: DietAgentState) -> dict[str, Any]:
        """节点 6：最终校验与事实绑定回答生成。"""
        wf_state = state["workflow_state"]
        c4 = state["c4"]
        session_id = state["session_id"]
        lock_token = state["lock_token"]
        lost = state["lost"]
        tool_ctx = state["tool_context"]
        plans = state["feasible_menus"]
        request_id = state["request_id"]
        participant_refs = state["participant_refs"]
        intent = state.get("intent")

        allow_polish = (intent.intent != "restore") if intent else True
        wf_state = self._select_validate_answer(
            wf_state,
            tool_ctx,
            plans,
            request_id,
            participant_refs,
            c4,
            session_id,
            lock_token,
            lost,
            allow_polish=allow_polish,
        )
        return {"workflow_state": wf_state, "is_terminal": True}

    def _node_commit(self, state: DietAgentState) -> dict[str, Any]:
        """节点 7：事务性终态提交。"""
        qid = state.get("pending_clarification_to_consume")
        c4 = state.get("c4")
        session_id = state.get("session_id", "")
        lock_token = state.get("lock_token", "")
        transition = state.get("clarification_transition")
        final_status = self._finalize(
            state["workflow_state"],
            state["request_id"],
            state["c4"],
            state["lock_token"],
            clarification_question_id=qid,
            clarification_transition=transition,
        )
        if final_status is None:
            st = state.get("workflow_state")
            if st and st.status:
                final_status = st.status.value if hasattr(st.status, "value") else str(st.status)
        if (
            qid and final_status in ("completed", "needs_clarification")
            and c4 and hasattr(c4, "consume_pending_clarification")
        ):
            try:
                c4.consume_pending_clarification(session_id, question_id=qid, token=lock_token)
            except Exception:
                # 两种终态均已由 MySQL 权威提交；Redis 投影故障不能反向覆盖终态。
                # 下次选项回复会读取已提交 question_id 并拒绝重复应用。
                logger.exception("已提交请求的澄清事项 Redis 消费失败: request_id=%s", state["request_id"])

        if (
            final_status == "needs_clarification"
            and transition is not None
            and c4 and hasattr(c4, "store_pending_clarification")
        ):
            try:
                trans_dict = transition.model_dump() if hasattr(transition, "model_dump") else transition
                next_qid = trans_dict.get("next_question_id")
                pub = trans_dict.get("next_public_payload") or {}
                priv = trans_dict.get("next_private_snapshot") or {}
                clarification_record = {
                    "question_id": next_qid,
                    "session_id": session_id,
                    "diagnosis_code": pub.get("inquiry_category") or "NEEDS_CLARIFICATION",
                    "inquiry_reason": pub.get("question_text", ""),
                    "options": pub.get("options", []),
                    "option_modifications": priv.get("option_modifications", {}),
                    "query_plan_snapshot": priv.get("query_plan_snapshot"),
                    "current_menu_version": priv.get("current_menu_version"),
                    "created_at": time.time(),
                    "expires_at": trans_dict.get("next_expires_at") or (time.time() + 3600),
                    "status": "pending",
                }
                c4.store_pending_clarification(session_id, clarification_record, token=lock_token)
            except Exception:
                logger.exception("已提交新问题的 Redis 缓存写入失败: request_id=%s", state["request_id"])
        return {}

    def _node_finish_error(self, state: DietAgentState) -> dict[str, Any]:
        """错误终态节点。"""
        wf_state = state["workflow_state"]
        if wf_state.status != RequestStatus.FAILED:
            wf_state = self._fail(
                wf_state,
                state.get("error_code") or "INTERNAL_ERROR",
                state.get("error_message") or "流程执行异常终止",
                node=NodeType.MENU_DECISION,
            )
        self._finalize(
            wf_state,
            state["request_id"],
            state["c4"],
            state["lock_token"],
        )
        return {"workflow_state": wf_state}

    # ---- Agent 行动—观察循环节点与决策实现 (Section 12 R6) ----

    def _node_decide(self, state: DietAgentState) -> dict[str, Any]:
        """Agent 决策节点：基于当前观察与执行上下文，由模型（或确定性策略）自主决定下一步行动。"""
        wf_state = state["workflow_state"]
        c4 = state.get("c4")
        session_id = state.get("session_id", "")
        lock_token = state.get("lock_token", "")
        lost = state.get("lost")

        guard = self._guard_active(wf_state, c4, session_id, lock_token, lost)
        if guard is not None:
            return {"workflow_state": guard, "is_terminal": True}

        policy = state.get("policy")
        if policy is None:
            policy = AgentPolicy()

        # 检查决策轮次预算（最多 12 次）
        budget_res = policy.check_decision_budget()
        if not budget_res.allowed:
            wf_state = self._fail(
                wf_state,
                budget_res.error_code or "DECISION_BUDGET_EXCEEDED",
                budget_res.error_message,
                node=NodeType.MENU_DECISION,
            )
            return {
                "workflow_state": wf_state,
                "policy": policy,
                "is_terminal": True,
                "error_code": budget_res.error_code,
                "error_message": budget_res.error_message,
            }

        try:
            action = self._decide_next_action(state, policy)
        except ModelDecisionError as exc:
            wf_state = self._fail(
                wf_state,
                exc.error_code,
                exc.message,
                node=NodeType.MENU_DECISION,
            )
            return {
                "workflow_state": wf_state,
                "policy": policy,
                "is_terminal": True,
                "error_code": exc.error_code,
                "error_message": exc.message,
            }
        except Exception as exc:
            wf_state = self._fail(
                wf_state,
                "MODEL_DECISION_UNEXPECTED_ERROR",
                f"Agent 决策发生未预期异常: {exc}",
                node=NodeType.MENU_DECISION,
            )
            return {
                "workflow_state": wf_state,
                "policy": policy,
                "is_terminal": True,
                "error_code": "MODEL_DECISION_UNEXPECTED_ERROR",
                "error_message": str(exc),
            }

        # Gate 门卫准入校验（参数规范、白名单、前置条件与频次控制）
        execution_context = state.get("execution_context") or {}
        gate_res = policy.validate_action_gate(action, execution_context)
        if not gate_res.allowed:
            wf_state = self._fail(
                wf_state,
                gate_res.error_code or "GATE_REJECTED",
                gate_res.error_message,
                node=NodeType.MENU_DECISION,
            )
            return {
                "current_action": action,
                "workflow_state": wf_state,
                "policy": policy,
                "is_terminal": True,
                "error_code": gate_res.error_code,
                "error_message": gate_res.error_message,
            }

        return {
            "current_action": action,
            "policy": policy,
        }

    def _decide_next_action(self, state: DietAgentState, policy: AgentPolicy) -> AgentAction:
        """模型动作决策（生产失败显式 fail-closed，消除静默降级）。"""
        # 1. 优先调用测试/外部显式注入的脚本化模型决策
        if self._llm is not None and hasattr(self._llm, "decide_action"):
            custom = self._llm.decide_action(state)
            if isinstance(custom, AgentAction):
                custom = custom.model_dump()
            if isinstance(custom, dict):
                try:
                    return AgentAction(**custom)
                except Exception as exc:
                    raise ModelDecisionError(
                        "MODEL_ACTION_SCHEMA_INVALID",
                        f"注入的脚本化模型输出不符合 Action Schema: {exc}",
                    ) from exc
            raise ModelDecisionError(
                "MODEL_ACTION_SCHEMA_INVALID",
                f"注入的脚本化模型返回了无效类型: {type(custom)}",
            )

        # 2. 检查模型客户端配置（生产 LangGraph 模式严禁无模型静默运行）
        if self._llm is None:
            raise ModelDecisionError(
                "MODEL_NOT_CONFIGURED",
                "未配置 Agent 决策模型客户端 (llm is None)，生产环境严禁静默降级为确定性执行",
            )

        # 3. 检查模型客户端 invoke 能力
        if not hasattr(self._llm, "invoke"):
            raise ModelDecisionError(
                "MODEL_INVOCATION_UNSUPPORTED",
                f"模型客户端缺少 invoke 方法: {type(self._llm)}",
            )

        # 4. 调用模型进行自主决策
        locked_summary = None
        rejected_summary = None
        curr_items = {}
        c4 = state.get("c4")
        session_id = state.get("session_id", "")
        if c4 and session_id:
            sess = c4.get_session_state(session_id) or {}
            curr = sess.get("current_menu") or {}
            for it in (curr.get("items") or []):
                curr_items[it.get("recipe_id")] = it.get("name", str(it.get("recipe_id")))

        locked_ids = state.get("locked_recipe_ids") or []
        if locked_ids:
            locked_names = [f"{curr_items.get(rid, str(rid))}(ID:{rid})" for rid in locked_ids]
            locked_summary = ", ".join(locked_names)

        rejected_ids = state.get("rejected_recipe_ids") or []
        if rejected_ids:
            rejected_names = [f"{curr_items.get(rid, str(rid))}(ID:{rid})" for rid in rejected_ids]
            rejected_summary = ", ".join(rejected_names)

        prompt = format_agent_prompt(
            query_plan=state.get("query_plan"),
            observations=state.get("observations", []),
            current_menu_summary=self._summarize_current_menu(state),
            user_message=state.get("message"),
            intent=state.get("intent"),
            locked_recipes_summary=locked_summary,
            rejected_recipes_summary=rejected_summary,
            available_evidence_refs=(state.get("execution_context") or {}).get("known_evidence", set()),
        )

        t0 = time.perf_counter()
        try:
            import inspect
            sig = inspect.signature(self._llm.invoke)
            invoke_kwargs: dict[str, Any] = {}
            if "timeout_seconds" in sig.parameters:
                invoke_kwargs["timeout_seconds"] = 60.0
            if "role" in sig.parameters:
                resp = self._llm.invoke(
                    role="menu_decision",
                    system_prompt=AGENT_DECISION_SYSTEM_PROMPT,
                    user_message=prompt,
                    **invoke_kwargs,
                )
            else:
                resp = self._llm.invoke([
                    {"role": "system", "content": AGENT_DECISION_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ])
        except Exception as exc:
            logger.error("Agent LLM 决策调用失败: %s", exc)
            raise ModelDecisionError(
                "MODEL_INVOCATION_FAILED",
                f"Agent 决策模型调用失败 ({type(exc).__name__}): {exc}",
            ) from exc
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if hasattr(self, "_trace") and self._trace:
                model_cfg = getattr(self._llm, "_llm_config", None)
                if model_cfg and hasattr(model_cfg, "model_for_role"):
                    m_name = model_cfg.model_for_role("menu_decision")
                else:
                    m_name = getattr(self._llm, "model", "qwen3.8-max")
                self._trace.add_model_call(
                    role="menu_decision",
                    model=m_name,
                    elapsed_ms=elapsed_ms,
                )

        # 5. 解析模型响应内容
        if isinstance(resp, dict):
            content = resp.get("content", "")
        else:
            content = getattr(resp, "content", resp)

        if not isinstance(content, str) or not content.strip():
            raise ModelDecisionError(
                "MODEL_OUTPUT_EMPTY",
                f"Agent 决策模型返回内容为空: {resp!r}",
            )

        c_clean = content.strip()
        if c_clean.startswith("```json"):
            c_clean = c_clean[7:]
        if c_clean.startswith("```"):
            c_clean = c_clean[3:]
        if c_clean.endswith("```"):
            c_clean = c_clean[:-3]

        try:
            parsed = json.loads(c_clean.strip())
        except Exception as exc:
            logger.error("Agent LLM 决策返回非合法 JSON: %s, 原始输出: %s", exc, content[:200])
            raise ModelDecisionError(
                "MODEL_OUTPUT_INVALID_JSON",
                f"Agent 决策模型返回非合法 JSON: {exc}",
            ) from exc

        if not isinstance(parsed, dict):
            raise ModelDecisionError(
                "MODEL_OUTPUT_INVALID_JSON",
                f"Agent 决策模型返回 JSON 根对象必须为字典: {type(parsed)}",
            )

        try:
            return AgentAction(**parsed)
        except Exception as exc:
            logger.error("Agent LLM 决策 Action Schema 校验失败: %s, 内容: %s", exc, parsed)
            raise ModelDecisionError(
                "MODEL_ACTION_SCHEMA_INVALID",
                f"Agent 决策模型行动 Schema 校验失败: {exc}",
            ) from exc

    @staticmethod
    def _default_agent_decision(state: DietAgentState) -> AgentAction:
        """默认确定性 Agent 决策策略：根据观察历史严密闭环流转。"""
        observations = state.get("observations") or []
        intent = state.get("intent")
        qp = state.get("query_plan")

        # 检查最近观察是否有业务无解/协商需求
        if observations:
            last_obs = observations[-1]
            if last_obs.status == "no_solution":
                diag_code = last_obs.error_code or state.get("diagnosis_code") or "NEEDS_CLARIFICATION"
                reason = last_obs.message or state.get("inquiry_reason") or "无法满足当前要求，请调整"
                raw_opts = state.get("structured_options") or [
                    {"option_id": idx + 1, "text": opt, "modifications": {}}
                    for idx, opt in enumerate(state.get("inquiry_options") or ["放宽偏好重新推荐", "调整菜品数量重新推荐"])
                ]
                if len(raw_opts) < 2:
                    raw_opts.append({"option_id": len(raw_opts) + 1, "text": "调整菜品数量重新推荐", "modifications": {}})
                options = raw_opts[:3]
                return AgentAction(
                    action=ActionType.ASK_USER,
                    arguments={
                        "inquiry_category": diag_code,
                        "reason": reason,
                        "options": options,
                    },
                )

        # 恢复历史菜单场景
        if intent and intent.intent == "restore":
            has_audit = any(o.action == ActionType.AUDIT_RECIPE_HEALTH for o in observations)
            if not has_audit:
                c_ids = state.get("candidate_recipes", [])
                evidence_ref = f"history:{qp.input_fingerprint if qp else 'v1'}"
                state.setdefault("execution_context", {}).setdefault("known_evidence", set()).add(evidence_ref)
                return AgentAction(
                    action=ActionType.AUDIT_RECIPE_HEALTH,
                    arguments={"candidate_recipe_ids": list(c_ids)},
                    evidence_refs=[evidence_ref],
                )
            has_combine = any(o.action == ActionType.COMBINE_NUTRITIONAL_MENU for o in observations)
            if not has_combine:
                dish_count = qp.dish_count_requested if qp else len(state.get("candidate_recipes", []))
                audit_obs = next(o for o in observations if o.action == ActionType.AUDIT_RECIPE_HEALTH)
                refs = [audit_obs.evidence_ref] if audit_obs.evidence_ref else []
                return AgentAction(
                    action=ActionType.COMBINE_NUTRITIONAL_MENU,
                    arguments={"dish_count": dish_count},
                    evidence_refs=refs,
                )
            has_validate = any(o.action == ActionType.VALIDATE_SELECTED_MENU for o in observations)
            if not has_validate:
                plans = state.get("feasible_menus", [])
                if plans:
                    plan = plans[0]
                    combine_obs = next(o for o in observations if o.action == ActionType.COMBINE_NUTRITIONAL_MENU)
                    refs = [combine_obs.evidence_ref] if combine_obs.evidence_ref else []
                    return AgentAction(
                        action=ActionType.VALIDATE_SELECTED_MENU,
                        arguments={"plan_id": plan.plan_id, "recipe_ids": list(plan.recipe_ids)},
                        evidence_refs=refs,
                    )
            # 已验证，执行 finish
            plans = state.get("feasible_menus", [])
            plan_id = plans[0].plan_id if plans else "plan-1"
            val_obs = next((o for o in observations if o.action == ActionType.VALIDATE_SELECTED_MENU), None)
            val_ref = val_obs.evidence_ref if (val_obs and val_obs.evidence_ref) else "val:pass"
            return AgentAction(
                action=ActionType.FINISH,
                arguments={"plan_id": plan_id, "final_validation_ref": val_ref},
                evidence_refs=[val_ref],
            )

        # 常规推荐与替换场景
        has_search = any(o.action in (ActionType.SEARCH_CANDIDATES, ActionType.EXPAND_CANDIDATES) for o in observations)
        if not has_search:
            query = (
                LangGraphRecommendationOrchestrator._retrieval_query(intent)
                if intent
                else state.get("message", "")
            )
            return AgentAction(
                action=ActionType.SEARCH_CANDIDATES,
                arguments={"query": query},
            )

        has_audit = any(o.action == ActionType.AUDIT_RECIPE_HEALTH for o in observations)
        if not has_audit:
            c_ids = state.get("candidate_recipes", [])
            search_obs = next(o for o in observations if o.action in (ActionType.SEARCH_CANDIDATES, ActionType.EXPAND_CANDIDATES))
            refs = [search_obs.evidence_ref] if search_obs.evidence_ref else []
            return AgentAction(
                action=ActionType.AUDIT_RECIPE_HEALTH,
                arguments={"candidate_recipe_ids": list(c_ids)},
                evidence_refs=refs,
            )

        has_combine = any(o.action == ActionType.COMBINE_NUTRITIONAL_MENU for o in observations)
        if not has_combine:
            dish_count = qp.dish_count_requested if qp else 5
            audit_obs = next(o for o in observations if o.action == ActionType.AUDIT_RECIPE_HEALTH)
            refs = [audit_obs.evidence_ref] if audit_obs.evidence_ref else []
            return AgentAction(
                action=ActionType.COMBINE_NUTRITIONAL_MENU,
                arguments={"dish_count": dish_count},
                evidence_refs=refs,
            )

        has_validate = any(o.action == ActionType.VALIDATE_SELECTED_MENU for o in observations)
        if not has_validate:
            plans = state.get("feasible_menus", [])
            if not plans:
                cur_count = qp.dish_count_requested if (qp and qp.dish_count_requested) else 3
                reduced_count = max(1, cur_count - 1)
                return AgentAction(
                    action=ActionType.ASK_USER,
                    arguments={
                        "inquiry_category": "NO_FEASIBLE_MENU",
                        "reason": "未能生成符合条件的可行方案",
                        "options": [
                            {"option_id": 1, "text": "放宽时间偏好重新推荐", "modifications": {"time_constraint_policy": "relax"}},
                            {"option_id": 2, "text": f"将菜品数量减少为{reduced_count}道重新推荐", "modifications": {"dish_count_requested": reduced_count}},
                        ],
                    },
                )
            plan = plans[0]
            combine_obs = next(o for o in observations if o.action == ActionType.COMBINE_NUTRITIONAL_MENU)
            refs = [combine_obs.evidence_ref] if combine_obs.evidence_ref else []
            return AgentAction(
                action=ActionType.VALIDATE_SELECTED_MENU,
                arguments={"plan_id": plan.plan_id, "recipe_ids": list(plan.recipe_ids)},
                evidence_refs=refs,
            )

        # 已验证，执行 finish
        plans = state.get("feasible_menus", [])
        plan_id = plans[0].plan_id if plans else "plan-1"
        val_obs = next((o for o in observations if o.action == ActionType.VALIDATE_SELECTED_MENU), None)
        val_ref = val_obs.evidence_ref if (val_obs and val_obs.evidence_ref) else "val:pass"
        return AgentAction(
            action=ActionType.FINISH,
            arguments={"plan_id": plan_id, "final_validation_ref": val_ref},
            evidence_refs=[val_ref],
        )

    @staticmethod
    def _summarize_current_menu(state: DietAgentState) -> str | None:
        c4 = state.get("c4")
        session_id = state.get("session_id", "")
        if not c4 or not session_id:
            return None
        sess = c4.get_session_state(session_id) or {}
        curr = sess.get("current_menu")
        if not curr:
            return None
        items = curr.get("items", [])
        names = [str(it.get("name", it.get("recipe_id"))) for it in items]
        return f"已提交方案 ID: {curr.get('plan_id')}，包含菜品: {', '.join(names)}"

    def _execute_final_validation(
        self, state: DietAgentState, action: AgentAction,
        execution_context: dict[str, Any],
    ) -> tuple[dict[str, Any], Observation]:
        """Consume only a fresh, request- and menu-bound final validation result."""
        tool_ctx = state["tool_context"]
        previous = tool_ctx.previous_results.pop("final_validation", None)
        old_ref = execution_context.pop("final_validation_ref", None)
        execution_context.pop("final_validation", None)
        if old_ref:
            execution_context.setdefault("known_evidence", set()).discard(old_ref)
        res: dict[str, Any] = {"final_validation": None, "tool_context": tool_ctx}

        def error(code: str, message: str) -> tuple[dict[str, Any], Observation]:
            tool_ctx.previous_results.pop("final_validation", None)
            return res, Observation(
                action=action.action, status="error", error_code=code, message=message,
            )

        plan_id = action.arguments.get("plan_id")
        recipe_ids = list(action.arguments.get("recipe_ids", []))
        plan = next((
            p for p in tool_ctx.previous_results.get("feasible_menus", [])
            if p.plan_id == plan_id
        ), None)
        if plan is None:
            return error("UNKNOWN_PLAN_ID", "最终校验必须引用当前可行方案")
        if len(recipe_ids) != len(set(recipe_ids)) or set(recipe_ids) != set(plan.recipe_ids):
            return error("MENU_HASH_MISMATCH", "校验菜品与所选可行方案不一致")

        tool_ctx.node_id = self._NODE_DECISION
        result = ToolHandler(tool_ctx).execute(
            "validate_selected_menu_health",
            {"plan_id": plan_id, "recipe_ids": recipe_ids},
        )
        if not isinstance(result, dict):
            return error("TOOL_RECEIPT_VALIDATION_FAILED", "最终校验未返回有效工具结果")
        if "error" in result:
            return error(str(result["error"]), "最终健康校验工具执行失败")
        fv = tool_ctx.previous_results.get("final_validation")
        if not isinstance(fv, FinalValidationArtifact):
            return error("FINAL_HEALTH_VALIDATION_MISSING", "本次调用缺少最终健康校验产物")
        if (
            (previous is not None and fv.artifact_id == getattr(previous, "artifact_id", None))
            or str(fv.request_id) != str(state["request_id"])
            or fv.plan_id != plan_id
            or len(fv.recipe_ids) != len(recipe_ids)
            or set(fv.recipe_ids) != set(recipe_ids)
            or set(fv.participant_refs) != set(state["participant_refs"])
            or fv.menu_hash != plan.menu_hash
            or result.get("verdict") != fv.status
            or result.get("plan_id") != fv.plan_id
            or result.get("recipe_ids") != list(fv.recipe_ids)
            or result.get("menu_hash") != fv.menu_hash
            or result.get("final_validation_ref") != str(fv.artifact_id)
        ):
            return error("ARTIFACT_INTEGRITY_FAILED", "本次校验产物与请求、菜单或工具回执不一致")

        evidence_ref = str(fv.artifact_id)
        execution_context.setdefault("known_evidence", set()).add(evidence_ref)
        if fv.status == "PASS":
            execution_context["final_validation"] = fv
            execution_context["final_validation_ref"] = evidence_ref
            res["final_validation"] = fv
            return res, Observation(
                action=action.action, status="ok", evidence_ref=evidence_ref,
                desensitized_facts={
                    "plan_id": fv.plan_id, "validation_status": "PASS", "menu_hash": fv.menu_hash,
                },
            )
        if fv.status != "EXCLUDE":
            return error("FINAL_HEALTH_VALIDATION_FAILED", "最终健康校验状态无效")

        # EXCLUDE invalidates the earlier candidate audit; a new B4 audit is mandatory.
        execution_context.update(
            is_health_revision=True, last_health_validation_status="EXCLUDE",
            health_evaluated=False, safe_recipe_ids=set(), feasible_plan_ids=set(),
        )
        for name in ("health_evaluation", "feasible_menus", "feasible_menu_artifact"):
            tool_ctx.previous_results.pop(name, None)
        tool_ctx.safe_recipe_ids = []
        res.update(safe_recipe_ids=[], feasible_menus=[], health_evaluation=None)
        return res, Observation(
            action=action.action, status="no_solution", error_code="VALIDATION_FAILED",
            message="最终健康校验为 EXCLUDE；旧候选审核和方案失效，必须重新审核候选后再规划",
            desensitized_facts={"requires_health_audit": True}, evidence_ref=evidence_ref,
        )

    def _node_execute_tool(self, state: DietAgentState) -> dict[str, Any]:
        """Agent 工具执行节点：由 Gate 准入后，统一调用底层工具并生成脱敏 Observation。"""
        wf_state = state["workflow_state"]
        c4 = state.get("c4")
        session_id = state.get("session_id", "")
        lock_token = state.get("lock_token", "")
        lost = state.get("lost")

        guard = self._guard_active(wf_state, c4, session_id, lock_token, lost)
        if guard is not None:
            return {"workflow_state": guard, "is_terminal": True}

        action = state.get("current_action")
        if not action:
            wf_state = self._fail(
                wf_state, "NO_ACTION_TO_EXECUTE", "没有可执行的动作", node=NodeType.MENU_DECISION
            )
            return {"workflow_state": wf_state, "is_terminal": True}

        policy = state.get("policy") or AgentPolicy()
        observations = list(state.get("observations") or [])
        execution_context = dict(state.get("execution_context") or {})
        policy.record_tool_execution(action, execution_context)

        tool_ctx = state["tool_context"]
        res: dict[str, Any] = {}

        def _invalidate_final_validation() -> None:
            tool_ctx.previous_results.pop("final_validation", None)
            old_ref = execution_context.pop("final_validation_ref", None)
            execution_context.pop("final_validation", None)
            if old_ref:
                execution_context.setdefault("known_evidence", set()).discard(old_ref)
            res["final_validation"] = None

        def _invalidate_feasible_plans() -> None:
            _invalidate_final_validation()
            tool_ctx.previous_results.pop("feasible_menus", None)
            tool_ctx.previous_results.pop("feasible_menu_artifact", None)
            execution_context["feasible_plan_ids"] = set()
            res["feasible_menus"] = []

        def _invalidate_health_evaluation() -> None:
            _invalidate_feasible_plans()
            tool_ctx.previous_results.pop("health_evaluation", None)
            tool_ctx.safe_recipe_ids = []
            execution_context["health_evaluated"] = False
            execution_context["safe_recipe_ids"] = set()
            res.update(safe_recipe_ids=[], health_evaluation=None)

        if action.action == ActionType.SEARCH_CANDIDATES:
            if "query" in action.arguments and state.get("intent"):
                state["intent"] = replace(state["intent"], rewritten_query=action.arguments["query"])
            res = self._node_search_candidates(state)
            if res.get("is_terminal"):
                _invalidate_health_evaluation()
                obs = Observation(
                    action=action.action,
                    status="error",
                    error_code=res.get("error_code"),
                    message=res.get("error_message") or "",
                )
            elif res.get("inquiry_needed"):
                _invalidate_health_evaluation()
                obs = Observation(
                    action=action.action,
                    status="no_solution",
                    error_code=res.get("diagnosis_code", "CANDIDATES_REQUIRED"),
                    desensitized_facts={"total_candidates": 0},
                    message=res.get("inquiry_reason") or "",
                )
            else:
                c_ids = res.get("candidate_recipes", [])
                _invalidate_health_evaluation()
                evidence_ref = f"retrieval:{len(c_ids)}"
                obs = Observation(
                    action=action.action,
                    status="ok",
                    desensitized_facts={"candidate_count": len(c_ids), "candidate_recipe_ids": list(c_ids)},
                    evidence_ref=evidence_ref,
                )
                execution_context.setdefault("known_candidates", set()).update(c_ids)
                execution_context.setdefault("known_evidence", set()).add(evidence_ref)

        elif action.action == ActionType.EXPAND_CANDIDATES:
            expand_query = action.arguments.get("query")
            if expand_query and state.get("intent"):
                state["intent"] = replace(state["intent"], rewritten_query=expand_query)
            res = self._node_search_candidates(state)
            if res.get("is_terminal"):
                _invalidate_health_evaluation()
                obs = Observation(
                    action=action.action,
                    status="error",
                    error_code=res.get("error_code"),
                    message=res.get("error_message") or "",
                )
            else:
                existing = state.get("candidate_recipes", [])
                new_c = res.get("candidate_recipes", [])
                merged = list(dict.fromkeys(existing + new_c))
                res["candidate_recipes"] = merged
                _invalidate_health_evaluation()
                evidence_ref = f"expansion:{len(merged)}"
                obs = Observation(
                    action=action.action,
                    status="ok",
                    desensitized_facts={"candidate_count": len(merged), "candidate_recipe_ids": list(merged)},
                    evidence_ref=evidence_ref,
                )
                execution_context.setdefault("known_candidates", set()).update(merged)
                execution_context.setdefault("known_evidence", set()).add(evidence_ref)

        elif action.action == ActionType.AUDIT_RECIPE_HEALTH:
            req_ids = action.arguments.get("candidate_recipe_ids")
            if req_ids:
                state["candidate_recipes"] = req_ids
            res = self._node_audit_health(state)
            if res.get("is_terminal"):
                _invalidate_feasible_plans()
                obs = Observation(
                    action=action.action,
                    status="error",
                    error_code=res.get("error_code"),
                    message=res.get("error_message") or "",
                )
            elif res.get("inquiry_needed"):
                _invalidate_feasible_plans()
                obs = Observation(
                    action=action.action,
                    status="no_solution",
                    error_code=res.get("diagnosis_code", "NO_SAFE_CANDIDATE"),
                    desensitized_facts={"safe_count": 0, "excluded_count": len(state.get("candidate_recipes", []))},
                    message=res.get("inquiry_reason") or "",
                )
            else:
                safe_ids = res.get("safe_recipe_ids", [])
                excluded_ids = res.get("excluded_recipe_ids", [])
                _invalidate_feasible_plans()
                evidence_ref = f"health_evaluation:{len(safe_ids)}"
                obs = Observation(
                    action=action.action,
                    status="ok",
                    desensitized_facts={
                        "safe_count": len(safe_ids),
                        "excluded_count": len(excluded_ids),
                        "safe_recipe_ids": list(safe_ids),
                    },
                    evidence_ref=evidence_ref,
                )
                execution_context["health_evaluated"] = True
                execution_context["safe_recipe_ids"] = set(safe_ids)
                execution_context.setdefault("known_evidence", set()).add(evidence_ref)

        elif action.action == ActionType.COMBINE_NUTRITIONAL_MENU:
            if "dish_count" in action.arguments and state.get("query_plan"):
                state["query_plan"] = state["query_plan"].model_copy(
                    update={"dish_count_requested": action.arguments["dish_count"]}
                )
            res = self._node_combine_menu(state)
            if res.get("is_terminal"):
                _invalidate_final_validation()
                execution_context["feasible_plan_ids"] = set()
                obs = Observation(
                    action=action.action,
                    status="error",
                    error_code=res.get("error_code"),
                    message=res.get("error_message") or "",
                )
            elif res.get("inquiry_needed"):
                _invalidate_final_validation()
                execution_context["feasible_plan_ids"] = set()
                obs = Observation(
                    action=action.action,
                    status="no_solution",
                    error_code=res.get("diagnosis_code", "NO_FEASIBLE_MENU"),
                    desensitized_facts={"diagnostics": res.get("diagnostics", {})},
                    message=res.get("inquiry_reason") or "",
                )
            else:
                plans = res.get("feasible_menus", [])
                _invalidate_final_validation()
                evidence_ref = f"feasible_menu:{len(plans)}"
                summaries = [{"plan_id": p.plan_id, "recipe_ids": list(p.recipe_ids)} for p in plans]
                obs = Observation(
                    action=action.action,
                    status="ok",
                    desensitized_facts={"feasible_plan_count": len(plans), "plans": summaries},
                    evidence_ref=evidence_ref,
                )
                execution_context["feasible_plan_ids"] = {
                    p.plan_id for p in plans if hasattr(p, "plan_id")
                }
                execution_context.setdefault("known_evidence", set()).add(evidence_ref)
                execution_context["is_health_revision"] = False

        elif action.action == ActionType.VALIDATE_SELECTED_MENU:
            res, obs = self._execute_final_validation(state, action, execution_context)

        elif action.action == ActionType.READ_MENU:
            target = action.arguments.get("target", "current")
            sess_state = c4.get_session_state(session_id) or {} if c4 else {}
            if target == "current":
                menu = sess_state.get("current_menu")
            else:
                hist = sess_state.get("menu_history") or []
                menu = hist[-2] if len(hist) >= 2 else None
            if not menu:
                obs = Observation(
                    action=action.action,
                    status="no_solution",
                    message=f"未找到目标菜单 ({target})",
                )
            else:
                r_ids = menu.get("recipe_ids", [])
                _invalidate_health_evaluation()
                evidence_ref = f"menu:{menu.get('plan_id')}"
                obs = Observation(
                    action=action.action,
                    status="ok",
                    desensitized_facts={
                        "target": target,
                        "plan_id": menu.get("plan_id"),
                        "recipe_ids": list(r_ids),
                        "dish_count": len(r_ids),
                    },
                    evidence_ref=evidence_ref,
                )
                execution_context.setdefault("known_candidates", set()).update(r_ids)
                execution_context.setdefault("known_evidence", set()).add(evidence_ref)
                res["candidate_recipes"] = list(r_ids)

        else:
            obs = Observation(
                action=action.action,
                status="error",
                error_code="UNAUTHORIZED_ACTION",
                message=f"未授权动作类型: {action.action}",
            )

        observations.append(obs)

        if obs.status == "error":
            wf_state = self._fail(
                wf_state,
                obs.error_code or "TOOL_EXECUTION_ERROR",
                obs.message or "工具执行失败",
                node=NodeType.MENU_DECISION,
            )
            return {
                **res,
                "workflow_state": wf_state,
                "is_terminal": True,
                "error_code": obs.error_code,
                "error_message": obs.message,
                "observations": observations,
                "policy": policy,
                "execution_context": execution_context,
            }

        return {
            **res,
            "inquiry_needed": False,
            "observations": observations,
            "policy": policy,
            "execution_context": execution_context,
        }

    # ---- 路由分支条件 ----

    @staticmethod
    def _route_after_intent(state: DietAgentState) -> str:
        if state.get("is_terminal"):
            return "error"
        if state.get("inquiry_needed"):
            return "inquire"
        return "decide"

    def _route_after_decide(self, state: DietAgentState) -> str:
        if state.get("is_terminal"):
            return "error"
        action = state.get("current_action")
        if not action:
            return "error"

        if action.action == ActionType.ASK_USER:
            return "inquire"

        if action.action == ActionType.FINISH:
            return "validate"

        return "execute"

    @staticmethod
    def _route_after_execute(state: DietAgentState) -> str:
        if state.get("is_terminal"):
            return "error"
        return "decide"

    @staticmethod
    def _route_after_search(state: DietAgentState) -> str:
        if state.get("is_terminal"):
            return "error"
        if state.get("inquiry_needed"):
            return "inquire"
        return "audit"

    @staticmethod
    def _route_after_audit(state: DietAgentState) -> str:
        if state.get("is_terminal"):
            return "error"
        if state.get("inquiry_needed"):
            return "inquire"
        return "combine"

    @staticmethod
    def _route_after_combine(state: DietAgentState) -> str:
        if state.get("is_terminal"):
            return "error"
        if state.get("inquiry_needed"):
            return "inquire"
        return "validate"

    # ---- LangGraph 图构建 ----

    def _build_graph(self):
        workflow = StateGraph(DietAgentState)

        workflow.add_node("understand_intent", self._node_understand_intent)
        workflow.add_node("decide", self._node_decide)
        workflow.add_node("execute_tool", self._node_execute_tool)
        workflow.add_node("inquire_user", self._node_inquire_user)
        workflow.add_node("validate_and_answer", self._node_validate_and_answer)
        workflow.add_node("commit", self._node_commit)
        workflow.add_node("finish_error", self._node_finish_error)

        # 兼容旧单测直接调用的具名节点
        workflow.add_node("search_candidates", self._node_search_candidates)
        workflow.add_node("audit_health", self._node_audit_health)
        workflow.add_node("combine_menu", self._node_combine_menu)

        workflow.add_edge(START, "understand_intent")

        workflow.add_conditional_edges(
            "understand_intent",
            self._route_after_intent,
            {
                "decide": "decide",
                "inquire": "inquire_user",
                "error": "finish_error",
            },
        )
        workflow.add_conditional_edges(
            "decide",
            self._route_after_decide,
            {
                "execute": "execute_tool",
                "inquire": "inquire_user",
                "validate": "validate_and_answer",
                "error": "finish_error",
            },
        )
        workflow.add_conditional_edges(
            "execute_tool",
            self._route_after_execute,
            {
                "decide": "decide",
                "inquire": "inquire_user",
                "error": "finish_error",
            },
        )

        workflow.add_edge("inquire_user", "commit")
        workflow.add_edge("validate_and_answer", "commit")
        workflow.add_edge("commit", END)
        workflow.add_edge("finish_error", END)

        return workflow.compile()

    # ---- 执行入口 ----

    def _run_locked(
        self,
        request_id: str,
        session_id: str,
        message: str,
        participants: list[dict],
        config: dict | None,
        c4: ContextService,
        lock_token: str,
        lost: Any,
        clarification_response: dict | None = None,
    ) -> None:
        if self._trace is None:
            self._trace = PerfTrace(request_id=request_id)

        build_id = self._resolve_build_id()
        participant_refs = [p["participant_ref"] for p in participants]
        user_id_mapping = {
            p["participant_ref"]: int(p["user_id"]) for p in participants
        }

        wf_state = WorkflowState(
            request_id=request_id,
            build_id=build_id,
            status=RequestStatus.RUNNING,
            participant_refs=participant_refs,
        )
        tool_ctx = ToolContext(
            request_id=request_id,
            build_id=build_id,
            participant_user_mapping=user_id_mapping,
            session_id=session_id,
            context_service=c4,
        )

        policy = AgentPolicy()
        execution_context = {
            "known_evidence": set(),
            "known_candidates": set(),
            "health_evaluated": False,
            "safe_recipe_ids": set(),
            "feasible_plan_ids": set(),
            "final_validation": None,
        }

        initial_state: DietAgentState = {
            "request_id": request_id,
            "session_id": session_id,
            "message": message,
            "participants": participants,
            "participant_refs": participant_refs,
            "user_id_mapping": user_id_mapping,
            "build_id": build_id,
            "lock_token": lock_token,
            "lost": lost,
            "c4": c4,
            "config": config,
            "workflow_state": wf_state,
            "tool_context": tool_ctx,
            "inquiry_needed": False,
            "inquiry_reason": "",
            "inquiry_options": [],
            "diagnosis_code": "",
            "diagnostics": {},
            "is_terminal": False,
            "error_code": None,
            "error_message": None,
            "candidate_recipes": [],
            "safe_recipe_ids": [],
            "excluded_recipe_ids": [],
            "health_excluded_details": {},
            "feasible_menus": [],
            "locked_recipe_ids": [],
            "rejected_recipe_ids": [],
            "policy": policy,
            "observations": [],
            "current_action": None,
            "execution_context": execution_context,
            "pending_clarification_to_consume": None,
            "clarification_response": clarification_response,
            "clarification_transition": None,
            "selected_option_id": None,
            "supersede_clarification": False,
        }

        self._graph.invoke(initial_state)
