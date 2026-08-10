"""C3 工作流运行器（T17）—— 有界状态机，fail-closed，严格 Artifact 契约。

- 所有 WorkflowState 更新一律经 `reduce_workflow_state`（纯 reducer），禁止原地修改；
- build_id 从唯一 ready 构建获取，注入 WorkflowState 与 ToolContext；每个节点入口注入真实 node_id；
- 每个模型节点输出按 RolePolicy.output_artifact_type 严格 `model_validate`；
  缺失字段、额外字段、普通文本、任意 dict 一律 `SCHEMA_VALIDATION_FAILED`；
- 最终健康校验只接受 B4 权威结果且 `verdict == PASS`，plan_id/recipe_ids/menu_hash
  必须与 MenuDecisionArtifact 完全绑定，禁止从截断 result_summary 拼造；
- Answer 必须解析正式 `AnswerArtifact` 并调用 `validate_answer_menu_binding`；
- 必需工具漏调/失败、未知 verdict、回执/Artifact 接地失败全部立即失败；
  无自动重试、不提示补齐必需工具、不切换模型、不模板回答、空输出立即失败。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from food_agent_v2.c3 import ROLE_POLICIES, NodeValidator, WorkflowError
from food_agent_v2.c3.llm_client import LLMClient, get_llm_client
from food_agent_v2.c3.prompts import get_prompt
from food_agent_v2.c3.state import (
    NodeType,
    RequestStatus,
    WorkflowState,
    reduce_workflow_state,
)
from food_agent_v2.c3.tool_handler import ToolContext, ToolHandler
from food_agent_v2.c4 import ContextService
from food_agent_v2.contracts.artifacts import (
    AnswerArtifact,
    ArtifactIntegrityError,
    FeasibleMenuArtifact,
    FinalValidationArtifact,
    HealthEvaluationArtifact,
    MenuDecisionArtifact,
    ParticipantRecipeHealthResult,
    QueryPlanArtifact,
    ReviewArtifact,
    validate_answer_menu_binding,
)
from food_agent_v2.contracts.receipts import ToolReceipt
from food_agent_v2.d1 import api as d1_api

#: RolePolicy.output_artifact_type → 严格 Pydantic Artifact 类（T03）。
ARTIFACT_TYPES: dict[str, type[BaseModel]] = {
    "QueryPlanArtifact": QueryPlanArtifact,
    "HealthEvaluationArtifact": HealthEvaluationArtifact,
    "FeasibleMenuArtifact": FeasibleMenuArtifact,
    "MenuDecisionArtifact": MenuDecisionArtifact,
    "FinalValidationArtifact": FinalValidationArtifact,
    "AnswerArtifact": AnswerArtifact,
    "ReviewArtifact": ReviewArtifact,
}


class WorkflowRunner:
    """有界状态机执行器：build_id/node_id 注入 + 纯 reducer + 严格 Artifact 契约。"""

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        build_id: str | None = None,
        build_provider: Any | None = None,
        c4: ContextService | None = None,
    ):
        self._llm = llm or get_llm_client()
        self._build_id = build_id
        self._build_provider = build_provider
        self._c4 = c4

    def _get_c4(self) -> ContextService:
        if self._c4 is None:
            self._c4 = ContextService()
        return self._c4

    def _resolve_build_id(self) -> str:
        """从唯一 ready 构建获取真实 build_id（非唯一即失败）。"""
        if self._build_id:
            return self._build_id
        if self._build_provider:
            return self._build_provider()
        from food_agent_v2.b3.repository import default_mysql_repository

        return default_mysql_repository().ready_build_id()

    def run(self, request_id: str, session_id: str,
            message: str, participants: list[dict],
            config: dict | None = None) -> None:
        """执行完整有界状态机。"""
        build_id = self._resolve_build_id()
        c4 = self._get_c4()
        participant_refs = [p["participant_ref"] for p in participants]
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

        # === 节点1: context_building ===
        state = self._enter_node(state, tool_ctx, NodeType.CONTEXT_BUILDING)

        from food_agent_v2.c3 import detect_untrusted_instruction

        _injection = detect_untrusted_instruction(message)
        if _injection:
            state = self._fail(state, "UNTRUSTED_INSTRUCTION_DETECTED",
                               f"检测到指令注入: {_injection}")
            self._finalize(state, request_id, c4)
            return

        ctx, _manifest = c4.build_shared_context(
            session_id, participant_refs,
            {"raw_text": message, "timestamp": time.time()},
            user_id_mapping,
            request_id=request_id,
        )
        state = reduce_workflow_state(
            state, action="set_context_ref", shared_context_ref=ctx.session_id)

        integrity = c4.validate_context_integrity(state.shared_context_ref)
        if not integrity.get("valid", False):
            state = self._fail(state, "CONTEXT_INTEGRITY_FAILED",
                               f"上下文完整性校验失败: {integrity.get('reason', 'core mismatch')}")
            self._finalize(state, request_id, c4)
            return

        d1_api.publish_analysis_event(request_id, "context_ready",
                                      f"已理解{len(participants)}位参与者的需求", [])
        state = reduce_workflow_state(state, action="context_building", manifest_valid=True)

        # === 有界状态机主循环 ===
        plan_id = ""
        recipe_ids: list[int] = []
        retrieved_ids: list[int] = []
        answer_text = ""
        md_artifact: MenuDecisionArtifact | None = None
        final_artifact: FinalValidationArtifact | None = None

        while state.current_node is not None and not state.is_terminal():
            node = state.current_node
            tool_ctx.node_id = node.value  # 每个节点入口注入真实 node_id

            if node == NodeType.QUERY_UNDERSTANDING:
                state, q_raw, q_artifact = self._run_model_node(
                    state, c4, tool_ctx, "query_understanding", user_message=message)
                if state.is_terminal():
                    break
                if isinstance(q_raw, dict) and q_raw.get("needs_clarification") is True:
                    state = reduce_workflow_state(
                        state, action="query_understanding", success=True,
                        needs_clarification=True)
                    d1_api.publish_clarification_event(request_id, q_raw)
                    break
                state = reduce_workflow_state(state, action="query_understanding", success=True)
                state = reduce_workflow_state(
                    state, action="set_artifact", artifact="query_plan", value=q_artifact)
                d1_api.publish_analysis_event(request_id, "query_understanding",
                                              "理解需求完成", [])
                _retrieval = tool_ctx.previous_results.get("retrieval")
                if _retrieval:
                    retrieved_ids = [c.recipe_id for c in _retrieval.candidates]
                tool_ctx.time_limit_minutes = None
                if q_artifact.time_constraint_seconds:
                    tool_ctx.time_limit_minutes = max(
                        1, round(q_artifact.time_constraint_seconds / 60))

            elif node == NodeType.HEALTH_MENU_PLANNING:
                hm_input = {
                    "query_plan": state.query_plan_artifact,
                    "retrieved_candidate_recipe_ids": retrieved_ids,
                }
                state, _hm_raw, hm_artifact = self._run_model_node(
                    state, c4, tool_ctx, "health_menu_planning",
                    user_message=json.dumps(hm_input, ensure_ascii=False),
                    handoff={"artifact_refs": ["query_plan", "retrieval"],
                             "action_required": "健康审查并生成菜单方案"},
                )
                if state.is_terminal():
                    break
                if hm_artifact is None:
                    state = self._fail(state, "HEALTH_MENU_PLANNING_EMPTY",
                                       "健康与菜单规划节点未产生结果")
                    break
                # 与历史行为一致：该节点输出同时作为健康评估/可行菜单 Artifact 登记
                state = reduce_workflow_state(
                    state, action="set_artifact", artifact="health_evaluation", value=hm_artifact)
                state = reduce_workflow_state(
                    state, action="set_artifact", artifact="feasible_menu", value=hm_artifact)
                d1_api.publish_analysis_event(request_id, "health_evaluation",
                                              "健康审查完成", [])
                d1_api.publish_analysis_event(request_id, "menu_planning",
                                              "菜单方案生成完成", [])
                state = reduce_workflow_state(state, action="health_menu_planning", result="ok")

            elif node == NodeType.MENU_DECISION:
                feasible = tool_ctx.previous_results.get("feasible_menus", [])
                md_input = {
                    "feasible_menus": [{
                        "plan_id": getattr(p, "plan_id", ""),
                        "recipe_ids": getattr(p, "recipe_ids", []),
                        "dominant_objective": getattr(p, "dominant_objective", ""),
                        "total_score": getattr(p, "total_score", 0.0),
                        "makespan_seconds": getattr(p, "makespan_seconds", None),
                    } for p in feasible],
                }
                state, _md_raw, _md_art = self._run_model_node(
                    state, c4, tool_ctx, "menu_decision",
                    user_message=json.dumps(md_input, ensure_ascii=False),
                    handoff={"artifact_refs": ["health_eval", "feasible_menus"],
                             "action_required": "选择最优方案并最终校验"},
                )
                if state.is_terminal():
                    break
                md: MenuDecisionArtifact = _md_art

                # FinalValidationArtifact 必须来自 B4 权威结果（禁止从截断 result_summary 拼造）
                fv = self._extract_final_validation(tool_ctx)
                if fv is None:
                    state = self._fail(state, "FINAL_HEALTH_VALIDATION_FAILED",
                                       "缺少 B4 最终健康校验结果")
                    break
                if fv["verdict"] not in ("PASS", "EXCLUDE"):
                    state = self._fail(state, "FINAL_HEALTH_VALIDATION_FAILED",
                                       f"未知最终校验 verdict: {fv['verdict']}")
                    break
                # B4 实际校验的 plan_id/recipe_ids/menu_hash 与 MenuDecisionArtifact 完全绑定
                if fv["plan_id"] != md.plan_id:
                    state = self._fail(state, "ARTIFACT_INTEGRITY_FAILED",
                                       "B4 校验 plan_id 与决策不一致")
                    break
                if set(fv["recipe_ids"]) != set(md.recipe_ids):
                    state = self._fail(state, "ARTIFACT_INTEGRITY_FAILED",
                                       "B4 校验 recipe_ids 与决策不一致")
                    break
                if fv["menu_hash"] != md.menu_hash:
                    state = self._fail(state, "ARTIFACT_INTEGRITY_FAILED",
                                       "B4 校验 menu_hash 与决策不一致")
                    break
                # 所选 plan_id 必须存在于可行方案（模型不能编造，文档 09 §8.4）
                feasible_ids = {getattr(p, "plan_id", "") for p in feasible}
                if md.plan_id not in feasible_ids:
                    state = self._fail(state, "ARTIFACT_INTEGRITY_FAILED",
                                       f"所选 plan_id 不在可行方案中: {md.plan_id}")
                    break

                if fv["verdict"] == "EXCLUDE":
                    state = reduce_workflow_state(state, action="menu_decision", needs_replan=True)
                    continue  # REVISING → health_menu_planning，或耗尽 → FAILED

                # 仅 PASS 可进入 answer_generation（fail-closed）
                state = reduce_workflow_state(state, action="menu_decision", validation_pass=True)
                md_artifact = md
                plan_id = md.plan_id
                recipe_ids = list(md.recipe_ids)

                # 构造权威 FinalValidationArtifact（INV-001）
                final_artifact = self._build_final_validation_artifact(state, fv, md)
                state = reduce_workflow_state(
                    state, action="set_artifact", artifact="final_validation",
                    value=final_artifact)

                c4_ctx = c4._sessions.get(state.shared_context_ref or "")
                if c4_ctx:
                    c4_ctx.current_menu.plan_id = plan_id
                    c4_ctx.current_menu.recipe_ids = recipe_ids
                    c4._persist_session(c4_ctx)
                d1_api.publish_analysis_event(request_id, "menu_decision",
                                              "菜单方案已选定", [])

            elif node == NodeType.ANSWER_GENERATION:
                feedback_text = ""
                if state.status == RequestStatus.REVISING and isinstance(state.review_artifact, ReviewArtifact):
                    issue_codes = state.review_artifact.issue_codes
                    if issue_codes:
                        feedback_text = (
                            f"\n\n## 上次审查反馈\n{json.dumps(list(issue_codes), ensure_ascii=False)}\n"
                            f"请修正后重新输出。")

                _answer_base = self._build_answer_base(
                    recipe_ids, plan_id, tool_ctx, md_artifact, final_artifact)
                state, _ans_raw, _ans_art = self._run_model_node(
                    state, c4, tool_ctx, "answer_generation",
                    user_message=json.dumps(_answer_base, ensure_ascii=False) + feedback_text,
                    handoff={"artifact_refs": ["menu_decision", "final_validation"],
                             "action_required": "生成用户可见回答"},
                )
                if state.is_terminal():
                    break
                ans: AnswerArtifact = _ans_art
                answer_text = self._artifact_answer_text(ans)
                state = reduce_workflow_state(
                    state, action="set_artifact", artifact="answer", value=ans)

                # INV-005：validate_answer_menu_binding（plan_id/recipe_ids/menu_hash/menu_ref/final_validation_ref/PASS）
                binding_err = self._answer_binding_error(ans, md_artifact, final_artifact)
                if binding_err:
                    state = self._fail(state, binding_err.error_code, binding_err.message)
                    break
                state = reduce_workflow_state(state, action="answer_generation", success=True)

            elif node == NodeType.UNIFIED_REVIEW:
                state, _rv_raw, _rv_art = self._run_model_node(
                    state, c4, tool_ctx, "unified_review",
                    user_message=answer_text,
                    handoff={"artifact_refs": ["answer"],
                             "action_required": "审查回答是否符合规则"},
                )
                if state.is_terminal():
                    break
                rv: ReviewArtifact = _rv_art
                state = reduce_workflow_state(
                    state, action="set_artifact", artifact="review", value=rv)
                # ReviewArtifact 使用正式 status 字段（PASS/REVISION_REQUIRED）
                state = reduce_workflow_state(state, action="unified_review", status=rv.status)

            elif node == NodeType.ATOMIC_COMMIT:
                break

            else:
                state = self._fail(state, "UNKNOWN_NODE", f"未知节点: {node}")
                break

        # 终态兜底：非终态不得以成功提交
        if not state.is_terminal():
            state = reduce_workflow_state(
                state, action="fail",
                error=WorkflowError("WORKFLOW_TERMINAL_VIOLATION",
                                    f"Non-terminal at commit: {state.status}"))

        state = reduce_workflow_state(state, action="atomic_commit")

        if state.status == RequestStatus.COMPLETED:
            fva = state.final_validation_artifact
            menu_hash = fva.menu_hash if fva else self._menu_hash(recipe_ids)
            ans = state.answer_artifact
            d1_api.publish_answer_event(
                request_id, answer_text, menu_hash,
                list(ans.evidence_refs) if isinstance(ans, AnswerArtifact) else [],
            )
            d1_api.publish_result_committed(
                request_id,
                {"menu": str(state.menu_decision_artifact)[:200],
                 "plan_id": plan_id,
                 "menu_hash": menu_hash},
            )

        self._finalize(state, request_id, c4)

    # ---- 状态机辅助 ----

    @staticmethod
    def _enter_node(state: WorkflowState, tool_ctx: ToolContext, node: NodeType) -> WorkflowState:
        """进入节点：更新 current_node 并注入真实 node_id。"""
        tool_ctx.node_id = node.value
        return reduce_workflow_state(state, action="set_node", node=node)

    @staticmethod
    def _fail(state: WorkflowState, code: str, message: str,
              node: NodeType | None = None) -> WorkflowState:
        """fail-closed：置为 FAILED 并记录确定性错误。"""
        return reduce_workflow_state(
            state, action="fail",
            error=WorkflowError(code, message, failed_node=node or state.current_node),
            node=node or state.current_node,
        )

    @staticmethod
    def _menu_hash(recipe_ids: list[int]) -> str:
        """最终菜单的规范 hash（Answer 与最终菜单接地）。"""
        canonical = json.dumps(sorted(int(r) for r in recipe_ids),
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _fingerprint(items: Any) -> str:
        return hashlib.sha256(
            json.dumps(items, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _run_model_node(self, state: WorkflowState, c4: ContextService,
                        tool_ctx: ToolContext, role: str, user_message: str,
                        handoff: dict | None = None) -> tuple[WorkflowState, dict, BaseModel | None]:
        """执行一个模型节点，返回 (新状态, 原始输出, 校验后的类型化 Artifact)。

        必需工具漏调/失败、模型异常、回执预算/身份失败、Artifact Schema 违约
        全部立即失败，无自动重试。
        """
        if self._is_cancelled(state.request_id):
            return reduce_workflow_state(state, action="set_status",
                                         status=RequestStatus.CANCELLED), {}, None
        policy = ROLE_POLICIES[role]

        pre_err = NodeValidator.pre_check(state, policy)
        if pre_err:
            return reduce_workflow_state(state, action="fail", error=pre_err), {}, None

        model_ctx = c4.project_model_context(role, handoff, state.shared_context_ref or "")
        pre_count = len(tool_ctx.tool_receipts)

        result = self._call_model(role, policy, model_ctx, user_message, tool_ctx)

        # 模型异常/空输出 → fail-closed（不切换模型、不模板回答、不 nudge 重试）
        if result.get("status") == "failed":
            err = WorkflowError("MODEL_CALL_FAILED",
                                result.get("error") or "模型调用失败",
                                failed_node=state.current_node)
            return reduce_workflow_state(state, action="fail", error=err), result, None

        # 查询理解：模型显式请求澄清时不校验 Artifact（澄清为终态）
        if role == "query_understanding" and isinstance(result, dict) \
                and result.get("needs_clarification") is True:
            return state, result, None

        node_receipts = tool_ctx.tool_receipts[pre_count:]

        budget_err = self._validate_tool_budget(node_receipts)
        if budget_err:
            return reduce_workflow_state(state, action="fail", error=budget_err), result, None

        post_err = NodeValidator.post_check(state, policy, result if result else None, node_receipts)
        if post_err:
            return reduce_workflow_state(state, action="fail", error=post_err), result, None

        # 严格 Artifact Schema 校验（缺失/额外/普通文本/任意 dict → SCHEMA_VALIDATION_FAILED）
        artifact, schema_err = self._validate_artifact(result, policy)
        if schema_err:
            return reduce_workflow_state(state, action="fail", error=schema_err), result, None

        state = reduce_workflow_state(
            state, action="record_receipts", receipts=self._as_authoritative_receipts(node_receipts))
        return state, result, artifact

    @staticmethod
    def _validate_artifact(parsed: Any, policy: Any) -> tuple[BaseModel | None, WorkflowError | None]:
        """按 RolePolicy.output_artifact_type 严格 model_validate；非契约输出一律拒绝。"""
        if not policy.output_artifact_type:
            return None, None
        type_names = [t.strip() for t in policy.output_artifact_type.split("|")]
        for name in type_names:
            cls = ARTIFACT_TYPES.get(name)
            if cls is None:
                return None, WorkflowError("SCHEMA_VALIDATION_FAILED", f"未知 Artifact 类型: {name}")
            try:
                return cls.model_validate(parsed), None
            except ValidationError:
                continue
        return None, WorkflowError(
            "SCHEMA_VALIDATION_FAILED",
            f"模型输出不符合 {policy.output_artifact_type} Schema",
        )

    @staticmethod
    def _validate_tool_budget(receipts: list[dict]) -> WorkflowError | None:
        """同一节点内同一 (tool_name, input_hash) 只能出现一次（预算 1）。"""
        seen: set[tuple[str, str]] = set()
        for r in receipts:
            key = (r.get("tool_name", ""), r.get("input_hash", ""))
            if key in seen:
                return WorkflowError("WORKFLOW_RETRY_LIMIT_EXCEEDED",
                                     f"同一节点内重复工具调用: {key}")
            seen.add(key)
        return None

    @staticmethod
    def _as_authoritative_receipts(receipts: list[dict]) -> list[ToolReceipt]:
        """把 tool_handler 产生的已绑定回执转换为权威 contracts ToolReceipt。"""
        out: list[ToolReceipt] = []
        for r in receipts:
            out.append(ToolReceipt(
                request_id=UUID(r["request_id"]),
                node_id=r["node_id"],
                tool_call_id=r["tool_call_id"],
                tool_name=r["tool_name"],
                input_hash=r["input_hash"],
                output_hash=r["output_hash"],
                build_id=UUID(r["build_id"]),
                success=r["success"],
                error_code=r["error_code"],
            ))
        return out

    # ---- 最终健康校验与绑定 ----

    @staticmethod
    def _extract_final_validation(tool_ctx: ToolContext) -> dict | None:
        """FinalValidationArtifact 必须来自 B4 权威结果（tool_handler 已存完整结果）。

        结果缺失或任一绑定键缺失/为空 → 视为无权威结果（fail-closed）。
        """
        fv = tool_ctx.previous_results.get("final_validation")
        if not isinstance(fv, dict):
            return None
        for key in ("verdict", "plan_id", "recipe_ids", "menu_hash"):
            if key not in fv or fv[key] in (None, ""):
                return None
        return fv

    @staticmethod
    def _build_final_validation_artifact(state: WorkflowState, fv: dict,
                                         md: MenuDecisionArtifact) -> FinalValidationArtifact:
        """从权威 B4 结果构造 FinalValidationArtifact（INV-001，不拼造）。"""
        participant_results = tuple(
            ParticipantRecipeHealthResult(
                participant_ref=pr["participant_ref"],
                recipe_id=int(pr["recipe_id"]),
                status=pr["status"],
                constraint_refs=(),
                evaluated_ingredient_ids=(),
                ingredient_set_evidence_paths=(),
                coverage_refs=(),
                exclusion_hits=(),
                input_fingerprint=WorkflowRunner._fingerprint(
                    [pr["recipe_id"], pr["participant_ref"]]),
            )
            for pr in fv.get("participant_recipe_results", [])
        )
        return FinalValidationArtifact(
            artifact_id=uuid.uuid4(),
            request_id=UUID(state.request_id),
            plan_id=fv["plan_id"],
            menu_artifact_ref=md.feasible_menu_artifact_ref,
            participant_refs=tuple(state.participant_refs),
            recipe_ids=tuple(int(r) for r in fv["recipe_ids"]),
            participant_recipe_results=participant_results,
            relation_evidence_refs=tuple(fv.get("evidence_refs", [])),
            menu_hash=fv["menu_hash"],
            input_fingerprint=WorkflowRunner._fingerprint([fv["menu_hash"]]),
            status=fv["verdict"],
        )

    @staticmethod
    def _answer_binding_error(answer: AnswerArtifact,
                              decision: MenuDecisionArtifact | None,
                              final: FinalValidationArtifact | None) -> WorkflowError | None:
        """INV-005：回答必须与最终菜单绑定（validate_answer_menu_binding）。"""
        if decision is None or final is None:
            return WorkflowError("ANSWER_GROUNDING_FAILED", "缺少决策/最终校验 Artifact")
        if not answer.recipe_ids:
            return WorkflowError("ANSWER_GROUNDING_FAILED",
                                 "回答未引用任何菜单菜品（recipe_ids 为空）")
        try:
            validate_answer_menu_binding(answer, decision, final)
        except ArtifactIntegrityError as e:
            return WorkflowError("ANSWER_GROUNDING_FAILED",
                                 f"回答与最终菜单绑定失败: {e.message}")
        problems = []
        if answer.menu_ref != decision.feasible_menu_artifact_ref:
            problems.append("menu_ref")
        if answer.final_validation_ref != str(final.artifact_id):
            problems.append("final_validation_ref")
        if problems:
            return WorkflowError("ANSWER_GROUNDING_FAILED", f"回答引用不一致: {problems}")
        return None

    def _call_model(self, role: str, policy: Any, model_ctx,
                    user_input: str, tool_ctx: ToolContext) -> dict:
        """调用 LLM——函数调用模式。

        只允许真实工具调用后的正常结果续接；不做必需工具提示补齐、不代调、
        不 nudge 空输出；空输出立即失败；循环耗尽即失败。
        """
        system_prompt = get_prompt(role)
        tool_defs = self._build_tool_defs(policy)
        tool_handler = ToolHandler(tool_ctx)

        ctx_data = {
            "role": model_ctx.role,
            "conversation": model_ctx.conversation_visible,
            "constraints": model_ctx.constraint_visible,
            "menu": model_ctx.menu_visible,
        }
        ctx_json = json.dumps(ctx_data, ensure_ascii=False, default=str)
        full_user = f"## 上下文\n{ctx_json}\n\n## 任务\n{user_input}"

        for _round in range(5):
            try:
                response = self._llm.invoke(
                    role, system_prompt, full_user,
                    tools=tool_defs if tool_defs else None,
                )
            except Exception as e:
                return {"status": "failed", "error": str(e), "content": ""}

            if response.get("_mock"):
                return {"status": "failed", "error": "MODEL_MOCK_RESPONSE", "content": ""}

            tool_calls = response.get("tool_calls", [])
            content = response.get("content", "")

            if tool_calls:
                # 真实工具调用 → 执行并把结果续接进上下文（只允许这种续接）
                tool_results = []
                allowed_names = {t.name for t in policy.allowed_tools}
                for tc in tool_calls:
                    name = tc.get("name", "")
                    args = tc.get("arguments", {})
                    if name not in allowed_names:
                        tool_results.append({"tool": name, "error": "TOOL_PERMISSION_DENIED"})
                        continue
                    result = tool_handler.execute(name, args)
                    tool_results.append({"tool": name, "result": result})

                results_text = "\n".join(
                    f"[{tr['tool']}] {json.dumps(tr.get('result', tr.get('error', '')), ensure_ascii=False, default=str)[:500]}"
                    for tr in tool_results
                )
                full_user += f"\n\n## 工具执行结果\n{results_text}"
                continue

            content_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if not content_text.strip():
                # 空输出立即失败（不 nudge、不模板回答）
                return {"status": "failed", "error": "MODEL_EMPTY_OUTPUT", "content": ""}

            return self._parse_result(content, role)

        # 循环耗尽仍无有效输出 → fail-closed
        return {"status": "failed", "error": "MODEL_NO_VALID_OUTPUT", "content": ""}

    def _build_tool_defs(self, policy: Any) -> list[dict] | None:
        if not policy.allowed_tools:
            return None

        tool_descriptions = {
            "retrieve_recipes": "从菜品知识库中检索候选菜品。参数query为自然语言查询文本。返回匹配的菜品列表及基础信息。当你需要获取候选菜品时必须调用此工具。",
            "get_current_menu": "获取当前会话已有的菜单方案（用于替换/恢复场景）。无需参数。",
            "get_health_constraints": "获取当前参与者的有效健康约束集（硬约束+软目标）。这是健康审查的前置步骤——必须先知道约束才能审查菜品。无需参数。",
            "evaluate_recipe_health": "对指定菜品执行逐参与者、逐菜品的健康审查。传入recipe_ids列表，返回safe_recipe_ids和excluded_recipe_ids。审查基于B4健康引擎的约束-食材关系表。",
            "generate_feasible_menus": "基于B4安全候选生成3-5个差异化的菜单方案。传入safe_recipe_ids和可选的dish_count。返回方案列表，每个方案含plan_id、recipe_ids、dominant_objective和makespan。",
            "expand_retrieval": "当前检索结果不足时，使用更宽松的查询进行二次检索。传入新query和已排除的original_ids。",
            "adjust_menu_plan": "调整已有菜单方案，替换指定菜品。传入plan_id和需要替换的recipe_id。",
            "validate_selected_menu_health": "对最终选定的菜单方案执行健康校验。重新加载B2当前约束和B3食材事实后进行最终审查。返回PASS或EXCLUDE。",
            "get_execution_trace": "获取当前请求的完整工具调用记录，包括调用角色、参数哈希和时间戳。用于审查工具合规性。",
            "get_artifact_chain": "获取当前请求的完整Artifact链引用和校验状态。用于审查证据完整性。",
        }

        tool_params = {
            "retrieve_recipes": {"query": {"type": "string", "description": "检索查询文本"}, "top_k": {"type": "integer", "description": "返回数量，默认20"}},
            "get_current_menu": {},
            "get_health_constraints": {},
            "evaluate_recipe_health": {"recipe_ids": {"type": "array", "items": {"type": "integer"}, "description": "待审查的菜品ID列表"}},
            "generate_feasible_menus": {"safe_recipe_ids": {"type": "array", "items": {"type": "integer"}, "description": "B4安全候选ID列表"}, "dish_count": {"type": "integer", "description": "目标菜数"}},
            "expand_retrieval": {"query": {"type": "string", "description": "更宽松的查询文本"}, "original_ids": {"type": "array", "items": {"type": "integer"}, "description": "已排除的菜品ID"}},
            "adjust_menu_plan": {"plan_id": {"type": "string", "description": "要调整的方案ID"}, "replace_recipe_id": {"type": "integer", "description": "要替换掉的菜品ID"}},
            "validate_selected_menu_health": {"plan_id": {"type": "string", "description": "方案ID"}, "recipe_ids": {"type": "array", "items": {"type": "integer"}, "description": "要校验的菜品ID列表"}},
            "get_execution_trace": {},
            "get_artifact_chain": {},
        }

        defs = []
        for t in policy.allowed_tools:
            params = tool_params.get(t.name, {})
            desc = tool_descriptions.get(t.name, f"Tool: {t.name}")
            defs.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": params,
                        "required": list(params.keys()) if params else [],
                    },
                },
            })
        return defs

    def _parse_result(self, content: str, role: str) -> dict:
        """解析模型输出为 dict。

        模型常把 JSON 包在 ```json 代码块或正文中，直接 json.loads 会失败。
        这里先尝试直接解析，再从 markdown 代码块/正文中提取 JSON 对象。
        """
        import re as _re

        if isinstance(content, dict):
            return content
        if content is None:
            return {"content": ""}
        try:
            result = json.loads(content)
            if isinstance(result, dict):
                return result
            return {"content": content}
        except (json.JSONDecodeError, TypeError):
            pass
        match = _re.search(r'\{.*\}', content, _re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass
        return {"content": content}

    # ---- 提取与构建辅助 ----

    @staticmethod
    def _build_answer_base(recipe_ids: list[int], plan_id: str,
                           tool_ctx: ToolContext, md: MenuDecisionArtifact | None,
                           final_artifact: FinalValidationArtifact | None) -> dict:
        """构建回答节点的公开事实基座：真实菜名 + 菜单 hash/引用 + 时间说明。"""
        try:
            from food_agent_v2.b3.recipe_views import get_view_builder
            _rbuilder = get_view_builder()
        except Exception:
            _rbuilder = None
        _dishes = []
        for rid in recipe_ids:
            _r = _rbuilder.get_recipe(rid) if _rbuilder else None
            _dishes.append({"recipe_id": rid, "name": (_r or {}).get("名称", "")})
        _makespan = None
        _time_source = "none"
        for _p in (tool_ctx.previous_results.get("feasible_menus", []) or []):
            if getattr(_p, "plan_id", "") == plan_id:
                _makespan = getattr(_p, "makespan_seconds", None)
                _time_source = getattr(_p, "time_source", "task_graph")
                break
        if _makespan and _time_source == "llm_estimate":
            _time_data = {"total_minutes": max(1, round(_makespan / 60)),
                          "available": True, "source": "llm_estimate"}
        else:
            _time_data = {"available": False}
        return {
            "selected_menu": {"plan_id": plan_id, "dishes": _dishes},
            "menu_hash": md.menu_hash if md else WorkflowRunner._menu_hash(recipe_ids),
            "menu_ref": md.feasible_menu_artifact_ref if md else "",
            "final_validation_ref": str(final_artifact.artifact_id) if final_artifact else "",
            "final_validation_status": final_artifact.status if final_artifact else "",
            "time_data": _time_data,
            "requested_time_limit_minutes": tool_ctx.time_limit_minutes,
        }

    @staticmethod
    def _artifact_answer_text(a: AnswerArtifact) -> str:
        """从正式 AnswerArtifact.content 提取用户可见回答文本。"""
        c = a.content
        parts = [c.conclusion, c.menu_summary, c.reasoning_summary,
                 c.health_note, c.time_note]
        return "\n".join(str(p) for p in parts if p and str(p).strip()).strip()

    @staticmethod
    def _is_cancelled(request_id: str) -> bool:
        """检查 D1 cancel 是否设置了 Redis 取消标记（文档 §15：节点边界检查）。"""
        try:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            store = RedisSessionStore()
            store._connect()
            if store._client:
                return bool(store._client.get(f"v2:cancel:{request_id}"))
        except Exception:
            pass
        return False

    def _finalize(self, state: WorkflowState, request_id: str,
                  c4: ContextService) -> None:
        """终态提交。不修改 state（runner 已保证终态）。"""
        effective_status = state.status.value
        error = state.error

        if effective_status == "completed":
            try:
                from food_agent_v2.application import commit_request_result
                fva = state.final_validation_artifact
                ans = state.answer_artifact
                health_evidence = {
                    "plan_id": fva.plan_id if isinstance(fva, FinalValidationArtifact) else "",
                    "recipe_ids": list(fva.recipe_ids) if isinstance(fva, FinalValidationArtifact) else [],
                    "menu_hash": fva.menu_hash if isinstance(fva, FinalValidationArtifact) else "",
                    "final_validation_verdict": fva.status if isinstance(fva, FinalValidationArtifact) else "",
                    "review_verdict": state.review_artifact.status
                    if isinstance(state.review_artifact, ReviewArtifact) else "",
                    "answer_nonempty": bool(ans.content.conclusion.strip())
                    if isinstance(ans, AnswerArtifact) else False,
                    "tool_receipt_count": len(state.tool_receipts),
                }
                commit_request_result(
                    request_id=request_id,
                    session_id=state.shared_context_ref or "",
                    status="completed",
                    final_plan_id=fva.plan_id if isinstance(fva, FinalValidationArtifact) else "",
                    health_evidence=health_evidence,
                    participant_refs=state.participant_refs,
                )
            except Exception as e:  # noqa: BLE001
                effective_status = RequestStatus.FAILED.value
                error = error or WorkflowError("AUDIT_COMMIT_FAILED", str(e))

        d1_api.update_status(request_id, effective_status,
                            result_summary={"status": effective_status},
                            error={"code": error.error_code,
                                   "message": error.message}
                            if error else None)
        c4.commit_session_state(request_id, effective_status)
