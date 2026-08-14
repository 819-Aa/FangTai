"""C3 确定性主编排器（P4）—— 直接连接 C1/B2/B4/C2，替代五模型在线主链。

继承 WorkflowRunner 复用：会话锁接线、context_building、_finalize 提交、
_build_dual_artifacts、R-002 临时约束闭环与 P1 性能观测。仅 override
``_run_locked``，把五模型 while 循环替换为确定性链路：

    FastIntentRouter → retrieve → evaluate_recipe_health → generate_feasible_menus
    → 确定性选优 → validate_selected_menu_health → AuthoritativeAnswerBuilder

健康与菜单校验完整保留，但不再由模型决定是否执行。多轮 delta（replace/reject）
在 P5 落地前 fallback 到 legacy 五模型链路（super()._run_locked）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from uuid import UUID

from food_agent_v2.c3 import detect_untrusted_instruction
from food_agent_v2.c3.authoritative_answer import AuthoritativeAnswerBuilder
from food_agent_v2.c3.fast_intent import FastIntentRouter
from food_agent_v2.c3.perf import PerfTrace
from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.state import (
    NodeType,
    RequestStatus,
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

#: generate_feasible_menus 返回的业务终态 → reducer result 映射。
_GENERATE_TERMINAL = {
    "no_safe_menu": "no_safe_menu",
    "no_feasible_menu": "no_feasible_menu",
    "strict_time_indeterminate": "strict_time_indeterminate",
}


class DeterministicRecommendationOrchestrator(WorkflowRunner):
    """确定性主编排器：单轮首次推荐走确定性链路，多轮 delta fallback legacy。"""

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

    def _build_query_plan(self, intent, request_id: str,
                          participant_refs: list[str]) -> QueryPlanArtifact:
        qp = QueryPlanArtifact(
            artifact_id=uuid.uuid4(),
            request_id=UUID(request_id),
            participant_refs=tuple(participant_refs),
            flavor_preferences=intent.flavor_preferences,
            dish_types=intent.dish_types,
            dish_count_requested=intent.dish_count_requested,
            health_exclusions=intent.health_exclusions,
            preference_exclusions=intent.preference_exclusions,
            time_constraint_seconds=intent.time_constraint_seconds,
            time_constraint_policy=intent.time_constraint_policy,
            input_fingerprint=canonical_json_hash(
                {"request_id": request_id, "participant_refs": participant_refs}),
            content_hash="0" * 64,
        )
        return qp.model_copy(update={"content_hash": self._content_hash(qp)})

    def _run_locked(self, request_id: str, session_id: str,
                    message: str, participants: list[dict],
                    config: dict | None, c4: ContextService,
                    lock_token: str, lost: Any) -> None:
        if self._trace is None:
            self._trace = PerfTrace(request_id=request_id)

        participant_refs = [p["participant_ref"] for p in participants]

        # 多轮（session 已有前文已提交菜单）或 replace/reject 意图，在 P5 落地前
        # fallback legacy 五模型主链——确定性链路不保留前文菜单，不能安全处理追加。
        intent = FastIntentRouter.route(
            message, participant_refs[0] if participant_refs else "p1")
        if intent.intent in ("replace", "reject_plan") or self._has_current_menu(c4, session_id):
            super()._run_locked(request_id, session_id, message, participants,
                                config, c4, lock_token, lost)
            return

        build_id = self._resolve_build_id()
        user_id_mapping = {p["participant_ref"]: int(p["user_id"]) for p in participants}

        state = WorkflowState(
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

        # === context_building（复用 legacy 逻辑）===
        state = self._enter_node(state, tool_ctx, NodeType.CONTEXT_BUILDING)
        self._trace.mark_node_start(NodeType.CONTEXT_BUILDING.value)

        _injection = detect_untrusted_instruction(message)
        if _injection:
            state = self._fail(state, "UNTRUSTED_INSTRUCTION_DETECTED",
                               f"检测到指令注入: {_injection}")
            self._finalize(state, request_id, c4, lock_token)
            return

        try:
            ctx, _manifest = c4.build_shared_context(
                session_id, participant_refs,
                {"raw_text": message, "timestamp": time.time()},
                user_id_mapping, request_id=request_id, build_id=build_id,
            )
        except PermanentConstraintLoadFailed as exc:
            state = self._fail(state, "PERMANENT_CONSTRAINT_LOAD_FAILED", str(exc))
            self._finalize(state, request_id, c4, lock_token)
            return
        except ContextBudgetExceeded as exc:
            state = self._fail(state, "CONTEXT_BUDGET_EXCEEDED", str(exc))
            self._finalize(state, request_id, c4, lock_token)
            return
        state = reduce_workflow_state(
            state, action="set_context_ref", shared_context_ref=ctx.session_id)

        integrity = c4.validate_context_integrity(state.shared_context_ref)
        if not integrity.get("valid", False):
            state = self._fail(state, "CONTEXT_INTEGRITY_FAILED",
                               f"上下文完整性校验失败: {integrity.get('reason', 'core mismatch')}")
            self._finalize(state, request_id, c4, lock_token)
            return

        d1_api.publish_analysis_event(request_id, "context_ready",
                                      f"已理解{len(participants)}位参与者的需求", [])
        state = reduce_workflow_state(state, action="context_building", manifest_valid=True)
        self._trace.mark_node_end(NodeType.CONTEXT_BUILDING.value)

        # === 确定性链路（失败点返回终态 state，统一在此 finalize）===
        state = self._deterministic_chain(
            state, tool_ctx, intent, request_id, session_id, participant_refs,
            user_id_mapping, c4)
        self._finalize(state, request_id, c4, lock_token)

    def _deterministic_chain(self, state, tool_ctx, intent, request_id,
                             session_id, participant_refs, user_id_mapping, c4):
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
            tool_ctx.time_limit_minutes = max(1, round(qp.time_constraint_seconds / 60))
        d1_api.publish_analysis_event(request_id, "query_understanding",
                                      "理解需求完成", [])
        self._trace.mark_node_end(NodeType.QUERY_UNDERSTANDING.value)

        # 2. 检索
        tool_ctx.node_id = self._NODE_RETRIEVE
        handler.execute("retrieve_recipes", {"query": intent.query, "top_k": 40})
        retrieval = tool_ctx.previous_results.get("retrieval")
        candidate_ids = [c.recipe_id for c in getattr(retrieval, "candidates", []) or []]
        if not candidate_ids:
            return self._fail(state, "CANDIDATES_REQUIRED", "检索无候选")

        # 3. 健康审查
        self._trace.mark_node_start(NodeType.HEALTH_MENU_PLANNING.value)
        tool_ctx.node_id = self._NODE_HEALTH
        eval_result = handler.execute("evaluate_recipe_health", {"recipe_ids": candidate_ids})
        if isinstance(eval_result, dict) and "error" in eval_result:
            # 食材集合不完整等系统错误 → fail-closed，不得误报 no_safe_menu
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

        # 4. 生成可行菜单（业务终态由工具返回值 note 判定）
        gen = handler.execute("generate_feasible_menus",
                              {"safe_recipe_ids": safe_ids,
                               "dish_count": qp.dish_count_requested})
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

        # 5. 构建双 Artifact（HealthEvaluationArtifact + FeasibleMenuArtifact）
        state, feasible_artifact = self._build_dual_artifacts(state, tool_ctx)
        if state.is_terminal():
            return state
        self._trace.mark_node_end(NodeType.HEALTH_MENU_PLANNING.value)

        # 6. 确定性选优 + 最终校验
        self._trace.mark_node_start(NodeType.MENU_DECISION.value)
        tool_ctx.node_id = self._NODE_DECISION
        best = max(plans, key=lambda p: getattr(p, "total_score", 0.0))
        handler.execute("validate_selected_menu_health",
                        {"plan_id": best.plan_id, "recipe_ids": list(best.recipe_ids)})
        fv = tool_ctx.previous_results.get("final_validation")
        if not isinstance(fv, FinalValidationArtifact):
            return self._fail(state, "FINAL_HEALTH_VALIDATION_FAILED",
                              "缺少 B4 最终健康校验结果")
        if fv.status != "PASS":
            return self._fail(state, "FINAL_HEALTH_VALIDATION_FAILED",
                              f"最终校验 verdict: {fv.status}")

        # 7. MenuDecisionArtifact（确定性选优，绑定可行方案与最终校验）
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

        # 8. 确定性回答 + ReviewArtifact(PASS)
        self._trace.mark_node_start(NodeType.ANSWER_GENERATION.value)
        answer = AuthoritativeAnswerBuilder.build(md, fv, state.build_id)
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
        state = reduce_workflow_state(state, action="atomic_commit")
        return state
