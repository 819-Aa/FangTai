"""C3 工作流运行器（T17）—— 有界状态机，fail-closed，严格 Artifact 契约。

- 所有 WorkflowState 更新一律经 `reduce_workflow_state`（纯 reducer），禁止原地修改；
- build_id 从唯一 ready 构建获取，注入 WorkflowState 与 ToolContext；每个节点入口注入真实 node_id；
- 每个模型节点输出按 RolePolicy.output_artifact_type 严格 `model_validate`；
  缺失字段、额外字段、普通文本、任意 dict 一律 `SCHEMA_VALIDATION_FAILED`；
- 唯一权威 menu_hash = C2 `menu_hash_for(plan_id, recipe_ids)`，B4/Answer/SSE 全部复用；
- 最终健康校验只接受 B4 产出的权威 FinalValidationArtifact 且 `status == PASS`，
  plan_id/recipe_ids/menu_hash/引用与 MenuDecisionArtifact 完全绑定；
- health_menu_planning 从工具回执分别构建真实 HealthEvaluationArtifact 与 FeasibleMenuArtifact；
- 必需工具漏调/失败、未知 verdict、回执/Artifact 接地失败全部立即失败；
  无自动重试、不提示补齐必需工具、不切换模型、不模板回答、空输出立即失败。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from food_agent_v2.c2.schemas import menu_hash_for
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
from food_agent_v2.c4 import ContextService, PermanentConstraintLoadFailed
from food_agent_v2.contracts.artifacts import (
    AnswerArtifact,
    ArtifactIntegrityError,
    FeasibleMenu,
    FeasibleMenuArtifact,
    FinalValidationArtifact,
    HealthEvaluationArtifact,
    MenuDecisionArtifact,
    MenuScoreDecomposition,
    ParticipantRecipeHealthResult,
    QueryPlanArtifact,
    ReviewArtifact,
    validate_answer_menu_binding,
)
from food_agent_v2.contracts.build import canonical_json_hash
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

    #: 会话锁 TTL（秒）；heartbeat 按 LOCK_TTL/3 续租，保证单个模型调用不会失锁。
    LOCK_TTL = 30

    #: 输出嵌入随机身份（UUID ref / tool_call_id）的工具，其 output_hash 非确定性，
    #: 不进入健康审计信封（确定性证据见 _canonical_audit）。
    NONDETERMINISTIC_OUTPUT_TOOLS = {
        "validate_selected_menu_health",
        "get_execution_trace",
        "get_artifact_chain",
    }

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

    # ---- 会话锁接线（fencing token，覆盖读取/运行/提交）----

    @staticmethod
    def _acquire_session_lock(c4: ContextService, session_id: str) -> str | None:
        """获取会话锁；Redis 不可用/占用返回 None（fail-closed）。

        测试替身（c4 无锁支持）回退为时间戳正整数 token——生产永不经过此路径，
        且不向提交 API 暴露魔法字符串。
        """
        acquire = getattr(c4, "acquire_session_lock", None)
        if acquire is None:
            return str(int(time.time() * 1000))  # 测试替身：正整数、跨运行单调
        return acquire(session_id, "runner")

    @staticmethod
    def _renew_session_lock(c4: ContextService, session_id: str, token: str) -> bool:
        renew = getattr(c4, "renew_session_lock", None)
        if renew is None:
            return True  # 测试 Fake 无锁支持
        return renew(session_id, token)

    @staticmethod
    def _session_lock_held(c4: ContextService, session_id: str, token: str) -> bool:
        is_held = getattr(c4, "is_session_lock_held", None)
        if is_held is None:
            return True  # 测试 Fake 无锁支持
        return is_held(session_id, token)

    @staticmethod
    def _release_session_lock(c4: ContextService, session_id: str, token: str) -> None:
        release = getattr(c4, "release_session_lock", None)
        if release is None:
            return
        release(session_id, token)

    # ---- 锁心跳与绑定 ----

    @staticmethod
    def _bind_session_lock(c4: ContextService, session_id: str, token: str) -> None:
        bind = getattr(c4, "bind_session_lock", None)
        if bind is None:
            return
        bind(session_id, token)

    @staticmethod
    def _unbind_session_lock(c4: ContextService, session_id: str) -> None:
        unbind = getattr(c4, "unbind_session_lock", None)
        if unbind is None:
            return
        unbind(session_id)

    def _start_heartbeat(self, c4: ContextService, session_id: str, token: str,
                         lost: threading.Event, stop: threading.Event):
        """持锁期间启动独立 heartbeat，按 LOCK_TTL/3 原子续租（单模型调用可超 TTL）。"""
        renew = getattr(c4, "renew_session_lock", None)
        if renew is None:
            return None  # 测试 Fake 无锁支持 → 无 heartbeat
        interval = max(0.2, self.LOCK_TTL / 3.0)
        t = threading.Thread(
            target=self._heartbeat_loop, args=(renew, session_id, token, lost, stop, interval),
            daemon=True, name=f"c3-lock-heartbeat-{session_id}")
        t.start()
        return t

    @staticmethod
    def _heartbeat_loop(renew, session_id: str, token: str,
                        lost: threading.Event, stop: threading.Event,
                        interval: float) -> None:
        while not stop.is_set():
            try:
                if not renew(session_id, token):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return
            stop.wait(interval)

    @staticmethod
    def _stop_heartbeat(heartbeat, stop: threading.Event) -> None:
        if heartbeat is None:
            return
        stop.set()
        heartbeat.join(timeout=5)

    def _finalize_lock_failure(self, request_id: str, c4: ContextService) -> None:
        """锁不可用/被占用 → fail-closed 终态（不伪造放行）。"""
        state = WorkflowState(request_id=request_id, status=RequestStatus.FAILED)
        state.error = WorkflowError(
            "SESSION_LOCK_UNAVAILABLE",
            "会话锁不可用或被占用（Redis 不可用或并发请求）",
            failed_node=NodeType.CONTEXT_BUILDING)
        self._finalize(state, request_id, c4)

    def run(self, request_id: str, session_id: str,
            message: str, participants: list[dict],
            config: dict | None = None) -> None:
        """执行完整有界状态机（会话锁覆盖同一 session 的读取/运行/提交）。"""
        c4 = self._get_c4()
        lock_token = self._acquire_session_lock(c4, session_id)
        if lock_token is None:
            # Redis 不可用/锁被占用 → fail-closed（不伪造 token 放行）
            self._finalize_lock_failure(request_id, c4)
            return
        # 绑定锁到 C4：工具持久化/最终提交失锁即 fail-closed
        self._bind_session_lock(c4, session_id, lock_token)
        lost = threading.Event()
        stop = threading.Event()
        heartbeat = self._start_heartbeat(c4, session_id, lock_token, lost, stop)
        try:
            self._run_locked(request_id, session_id, message, participants,
                             config, c4, lock_token, lost)
        finally:
            # 先停并 join heartbeat，再原子释放（避免续租与释放竞态）
            self._stop_heartbeat(heartbeat, stop)
            self._unbind_session_lock(c4, session_id)
            self._release_session_lock(c4, session_id, lock_token)

    def _run_locked(self, request_id: str, session_id: str,
                    message: str, participants: list[dict],
                    config: dict | None, c4: ContextService,
                    lock_token: str, lost: threading.Event) -> None:
        """会话锁保护下的完整有界状态机体。"""
        build_id = self._resolve_build_id()
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
            self._finalize(state, request_id, c4, lock_token)
            return

        try:
            ctx, _manifest = c4.build_shared_context(
                session_id, participant_refs,
                {"raw_text": message, "timestamp": time.time()},
                user_id_mapping,
                request_id=request_id,
                # MC-02：本次 build_id 传到永久约束加载器（B2 只读该 ready 构建）
                build_id=build_id,
            )
        except PermanentConstraintLoadFailed as exc:
            # 约束先行 fail-closed：永久约束加载失败 → failed，不进模型节点
            state = self._fail(state, "PERMANENT_CONSTRAINT_LOAD_FAILED", str(exc))
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

        # === 有界状态机主循环 ===
        plan_id = ""
        recipe_ids: list[int] = []
        retrieved_ids: list[int] = []
        answer_text = ""
        md_artifact: MenuDecisionArtifact | None = None
        final_artifact: FinalValidationArtifact | None = None
        feasible_artifact: FeasibleMenuArtifact | None = None

        while state.current_node is not None and not state.is_terminal():
            # heartbeat 失锁标记：单节点执行超 TTL 也可能失锁 → fail
            if lost.is_set() or not self._renew_session_lock(c4, session_id, lock_token):
                state = self._fail(state, "SESSION_LOCK_LOST",
                                   "会话锁已失效（TTL 过期或被覆盖）")
                break
            node = state.current_node
            tool_ctx.node_id = node.value  # 每个节点入口注入真实 node_id

            if node == NodeType.QUERY_UNDERSTANDING:
                state, q_raw, q_artifact = self._run_model_node(
                    state,
                    c4,
                    tool_ctx,
                    "query_understanding",
                    user_message=message,
                    # Retrieval is a deterministic prerequisite, not a decision the
                    # language model may omit.  The model receives the authoritative
                    # result and remains responsible only for QueryPlan semantics.
                    deterministic_tools=[("retrieve_recipes", {"query": message})],
                )
                if state.is_terminal():
                    break
                # 查询理解输出必须为合法 QueryPlanArtifact（无澄清绕过；不满足即 fail-closed）
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
                # MC-01-R2 P0-1：节点入口清除旧健康回执，禁止复用前一轮/前一节点回执
                tool_ctx.previous_results.pop("health_evaluation", None)
                hm_input = {
                    "query_plan": state.query_plan_artifact,
                    "retrieved_candidate_recipe_ids": retrieved_ids,
                }
                state, _hm_raw, _hm_art = self._run_model_node(
                    state, c4, tool_ctx, "health_menu_planning",
                    user_message=json.dumps(hm_input, ensure_ascii=False, default=str),
                    handoff={"artifact_refs": ["query_plan", "retrieval"],
                             "action_required": "健康审查并生成菜单方案"},
                )
                if state.is_terminal():
                    break
                # 从工具回执分别构建真实双 Artifact（禁止二选一/字段非空混入错误类型）
                state, feasible_artifact = self._build_dual_artifacts(state, tool_ctx)
                if state.is_terminal():
                    break
                d1_api.publish_analysis_event(request_id, "health_evaluation",
                                              "健康审查完成", [])
                d1_api.publish_analysis_event(request_id, "menu_planning",
                                              "菜单方案生成完成", [])
                state = reduce_workflow_state(state, action="health_menu_planning", result="ok")

            elif node == NodeType.MENU_DECISION:
                feasible = tool_ctx.previous_results.get("feasible_menus", [])
                md_input = {
                    # 每个可行方案必须携带自己的 menu_hash（FeasibleMenuArtifact 无顶层 menu_hash）
                    "feasible_menus": [{
                        "plan_id": getattr(p, "plan_id", ""),
                        "recipe_ids": getattr(p, "recipe_ids", []),
                        "menu_hash": getattr(p, "menu_hash", ""),
                        "dominant_objective": getattr(p, "dominant_objective", ""),
                        "total_score": getattr(p, "total_score", 0.0),
                        "makespan_seconds": getattr(p, "makespan_seconds", None),
                    } for p in feasible],
                    "feasible_menu_artifact_ref": str(feasible_artifact.artifact_id)
                    if feasible_artifact else "",
                    "participant_refs": participant_refs,
                    "request_id": request_id,
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

                # FinalValidationArtifact 必须来自 B4 权威产出（可引用、禁止截断拼造）
                fv = self._extract_final_validation(tool_ctx)
                if fv is None:
                    state = self._fail(state, "FINAL_HEALTH_VALIDATION_FAILED",
                                       "缺少 B4 最终健康校验结果")
                    break
                if fv.status not in ("PASS", "EXCLUDE"):
                    state = self._fail(state, "FINAL_HEALTH_VALIDATION_FAILED",
                                       f"未知最终校验 verdict: {fv.status}")
                    break
                # B4 实际校验的 plan_id/recipe_ids/menu_hash 与 MenuDecisionArtifact 完全绑定
                if fv.plan_id != md.plan_id:
                    state = self._fail(state, "ARTIFACT_INTEGRITY_FAILED",
                                       "B4 校验 plan_id 与决策不一致")
                    break
                if set(fv.recipe_ids) != set(md.recipe_ids):
                    state = self._fail(state, "ARTIFACT_INTEGRITY_FAILED",
                                       "B4 校验 recipe_ids 与决策不一致")
                    break
                if fv.menu_hash != md.menu_hash:
                    state = self._fail(state, "ARTIFACT_INTEGRITY_FAILED",
                                       "B4 校验 menu_hash 与决策不一致")
                    break
                # 所选 plan_id 必须存在于可行方案（模型不能编造，文档 09 §8.4）
                feasible_ids = {getattr(p, "plan_id", "") for p in feasible}
                if md.plan_id not in feasible_ids:
                    state = self._fail(state, "ARTIFACT_INTEGRITY_FAILED",
                                       f"所选 plan_id 不在可行方案中: {md.plan_id}")
                    break
                # MenuDecisionArtifact.menu_hash 必须等于所选 plan 对应的 FeasibleMenu.menu_hash
                plan = next((p for p in feasible if getattr(p, "plan_id", "") == md.plan_id), None)
                if plan is None or md.menu_hash != getattr(plan, "menu_hash", ""):
                    state = self._fail(state, "ARTIFACT_INTEGRITY_FAILED",
                                       "决策 menu_hash 与所选可行方案不一致")
                    break
                # 引用生命周期：MenuDecisionArtifact 引用 B4 FinalValidationArtifact 与 FeasibleMenuArtifact
                refs_err = self._decision_refs_error(md, fv, feasible_artifact)
                if refs_err:
                    state = self._fail(state, refs_err.error_code, refs_err.message)
                    break

                if fv.status == "EXCLUDE":
                    state = reduce_workflow_state(state, action="menu_decision", needs_replan=True)
                    continue  # REVISING → health_menu_planning，或耗尽 → FAILED

                # 仅 PASS 可进入 answer_generation（fail-closed）
                state = reduce_workflow_state(state, action="menu_decision", validation_pass=True)
                md_artifact = md
                plan_id = md.plan_id
                recipe_ids = list(md.recipe_ids)
                final_artifact = fv
                state = reduce_workflow_state(
                    state, action="set_artifact", artifact="menu_decision", value=md)
                state = reduce_workflow_state(
                    state, action="set_artifact", artifact="final_validation", value=fv)

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

        # stale token 不得提交成功结果（fencing 校验；锁已被覆盖/过期 → failed）
        if state.status == RequestStatus.COMPLETED and \
                not self._session_lock_held(c4, session_id, lock_token):
            state = self._fail(state, "SESSION_LOCK_LOST",
                               "提交时会话锁已失效（stale token）")

        state = reduce_workflow_state(state, action="atomic_commit")

        # T19：success SSE 不再由 runner 直接发布；改由事务提交后的 outbox dispatcher 发布。
        # 提交所需 answer/menu/evidence 在 _finalize 内从 state 提取后传入 commit_request_result。

        self._finalize(state, request_id, c4, lock_token)

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
    def _menu_hash(plan_id: str, recipe_ids: list[int]) -> str:
        """唯一权威 menu_hash（C2，C3 不得另行定义）。"""
        return menu_hash_for(plan_id, recipe_ids)

    @staticmethod
    def _content_hash(artifact: BaseModel) -> str:
        """对 Artifact 语义内容做规范 hash（排除身份/指纹字段）。"""
        payload = artifact.model_dump(exclude={"content_hash", "input_fingerprint",
                                               "artifact_id", "request_id"})
        return canonical_json_hash(payload)

    def _build_dual_artifacts(self, state: WorkflowState,
                              tool_ctx: ToolContext) -> tuple[WorkflowState, FeasibleMenuArtifact | None]:
        """从工具回执分别构建真实 HealthEvaluationArtifact 与 FeasibleMenuArtifact。

        验证：FeasibleMenu 全部菜品 ⊆ HealthEvaluation.safe_recipe_ids；
        必需工具回执身份已由 post_check 校验。任一构建失败即 fail-closed。
        """
        receipt = tool_ctx.previous_results.get("health_evaluation")
        if receipt is None or not getattr(receipt, "safe_recipe_ids", None) is not None:
            return self._fail(state, "HEALTH_EVALUATION_REQUIRED",
                              "缺少权威健康评估回执"), None
        safe_ids = tuple(int(r) for r in (receipt.safe_recipe_ids or []))
        # MC-01：权威 safe 为空 → no_safe_menu（不构建 Artifact，不进入后续节点）
        if not safe_ids:
            return (reduce_workflow_state(
                state, action="health_menu_planning", result="no_safe_menu"), None)
        plans = tool_ctx.previous_results.get("feasible_menus", [])
        # MC-01：safe 非空但无可行方案 → no_feasible_menu（不得回退不安全候选）
        if not plans:
            return (reduce_workflow_state(
                state, action="health_menu_planning", result="no_feasible_menu"), None)
        try:
            health_artifact = HealthEvaluationArtifact(
                artifact_id=uuid.uuid4(),
                request_id=UUID(state.request_id),
                participant_refs=tuple(state.participant_refs),
                safe_recipe_ids=safe_ids,
                excluded_recipe_ids=tuple(int(r) for r in receipt.excluded_recipe_ids),
                participant_recipe_results=tuple(
                    ParticipantRecipeHealthResult(
                        participant_ref=r.participant_ref,
                        recipe_id=r.recipe_id,
                        status=r.verdict,
                        constraint_refs=tuple(r.evidence_refs),
                        evaluated_ingredient_ids=(),
                        ingredient_set_evidence_paths=(),
                        coverage_refs=(),
                        exclusion_hits=tuple(
                            h.get("constraint_code") or "" for h in r.hitting_constraints),
                        input_fingerprint=canonical_json_hash(
                            [r.recipe_id, r.participant_ref]),
                    )
                    for r in receipt.participant_recipe_results
                ),
                recipe_group_results=(),
                input_fingerprint=canonical_json_hash(sorted(safe_ids)),
                content_hash="0" * 64,
            )
            menus = tuple(
                FeasibleMenu(
                    plan_id=p.plan_id,
                    recipe_ids=tuple(int(r) for r in p.recipe_ids),
                    menu_hash=p.menu_hash,
                    score_decomposition=MenuScoreDecomposition(
                        total_score=p.total_score,
                        rag_score=0.0,
                        preference_score=p.preference_score,
                        nutrition_score=p.nutrition_score,
                        time_score=p.time_score,
                        diversity_score=p.diversity_score,
                        historical_score=0.0,
                    ),
                    differing_recipe_ids=tuple(int(r) for r in p.differing_recipe_ids),
                    dominant_objective=(p.dominant_objective
                                        if p.dominant_objective in
                                        ("balanced", "preference", "nutrition", "quick", "diverse")
                                        else "balanced"),
                    strict_time_feasible=p.strict_time_feasible,
                    makespan_seconds=p.makespan_seconds,
                )
                for p in plans
            )
            # 菜单集合关系：全部菜品 ⊆ 安全候选
            for m in menus:
                if set(m.recipe_ids) - set(safe_ids):
                    return self._fail(
                        state, "ARTIFACT_INTEGRITY_FAILED",
                        f"可行方案 {m.plan_id} 含非安全候选菜品"), None
            feasible_artifact = FeasibleMenuArtifact(
                artifact_id=uuid.uuid4(),
                request_id=UUID(state.request_id),
                safe_recipe_ids_ref=str(health_artifact.artifact_id),
                menus=menus,
                input_fingerprint=canonical_json_hash([m.plan_id for m in menus]),
                content_hash="0" * 64,
            )
        except (ValidationError, AttributeError, TypeError) as e:
            return self._fail(state, "SCHEMA_VALIDATION_FAILED",
                              f"健康/菜单 Artifact 构建失败: {e}"), None

        health_artifact = health_artifact.model_copy(
            update={"content_hash": self._content_hash(health_artifact)})
        feasible_artifact = feasible_artifact.model_copy(
            update={"content_hash": self._content_hash(feasible_artifact)})
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="health_evaluation", value=health_artifact)
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="feasible_menu", value=feasible_artifact)
        # 供 B4 最终校验引用 FeasibleMenuArtifact.id
        tool_ctx.previous_results["feasible_menu_artifact"] = feasible_artifact
        return state, feasible_artifact

    def _run_model_node(self, state: WorkflowState, c4: ContextService,
                        tool_ctx: ToolContext, role: str, user_message: str,
                        handoff: dict | None = None,
                        deterministic_tools: list[tuple[str, dict]] | None = None,
                        ) -> tuple[WorkflowState, dict, BaseModel | None]:
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

        deterministic_results: list[dict] = []
        if deterministic_tools:
            handler = ToolHandler(tool_ctx)
            for tool_name, arguments in deterministic_tools:
                deterministic_results.append({
                    "tool": tool_name,
                    "result": handler.execute(tool_name, arguments),
                })

        result = self._call_model(role, policy, model_ctx, user_message, tool_ctx,
                                  response_format=self._artifact_response_format(policy, role),
                                  already_executed={item["tool"] for item in deterministic_results},
                                  deterministic_results=deterministic_results)

        if result.get("status") == "failed":
            err = WorkflowError("MODEL_CALL_FAILED",
                                result.get("error") or "模型调用失败",
                                failed_node=state.current_node)
            return reduce_workflow_state(state, action="fail", error=err), result, None

        # MC-01-R2 P0-1：无论普通结果还是业务 terminal，都必须先统一执行本节点
        # 回执校验（budget / identity / required tool / record_receipts），
        # 全部通过后才能转换业务终态；任一失败 fail-closed，不得进入终态。
        node_receipts = tool_ctx.tool_receipts[pre_count:]

        budget_err = self._validate_tool_budget(node_receipts)
        if budget_err:
            return reduce_workflow_state(state, action="fail", error=budget_err), result, None

        post_err = NodeValidator.post_check(state, policy, result if result else None, node_receipts)
        if post_err:
            return reduce_workflow_state(state, action="fail", error=post_err), result, None

        if result.get("status") == "terminal":
            # 业务终态：校验通过 → 记录本节点回执到 WorkflowState，再转精确终态
            state = reduce_workflow_state(
                state, action="record_receipts",
                receipts=self._as_authoritative_receipts(node_receipts))
            terminal = result.get("terminal")
            return reduce_workflow_state(
                state, action="health_menu_planning", result=terminal), result, None

        # 真实 input_fingerprint：绑定本节点 request/node/role/ModelContext 投影与用户输入
        fingerprint_seed = self._fingerprint_seed(state, role, model_ctx, user_message)
        artifact, schema_err = self._assemble_artifact(
            result, policy, state.request_id, state.participant_refs, fingerprint_seed)
        if schema_err:
            return reduce_workflow_state(state, action="fail", error=schema_err), result, None

        state = reduce_workflow_state(
            state, action="record_receipts", receipts=self._as_authoritative_receipts(node_receipts))
        return state, result, artifact

    @staticmethod
    def _artifact_response_format(policy: Any, role: str) -> dict | None:
        """为模型传 response_format 强制 JSON 对象。

        当前 LLM 供应商（DeepSeek）不支持 json_schema；用 json_object 强制 JSON
        对象输出，严格 Artifact Schema 由 _validate_artifact 强制（fail-closed）。
        """
        return {"type": "json_object"}

    @staticmethod
    def _validate_artifact(parsed: Any, policy: Any) -> tuple[BaseModel | None, WorkflowError | None]:
        """按 RolePolicy.output_artifact_type 严格 model_validate（完整契约校验）。"""
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
    def _fingerprint_seed(state: WorkflowState, role: str, model_ctx: Any,
                          user_message: str) -> str:
        """绑定本节点真实输入的指纹种子（request/node/role/ModelContext 投影/用户输入）。"""
        projection = {
            "role": getattr(model_ctx, "role", role),
            "conversation": getattr(model_ctx, "conversation_visible", None),
            "constraints": getattr(model_ctx, "constraint_visible", None),
            "menu": getattr(model_ctx, "menu_visible", None),
        }
        return canonical_json_hash({
            "request_id": state.request_id,
            "node_id": state.current_node.value if state.current_node else "",
            "build_id": state.build_id,
            "role": role,
            "projection": projection,
            "user_message": user_message,
        })

    def _assemble_artifact(self, parsed: Any, policy: Any,
                           request_id: str, participant_refs: list[str],
                           fingerprint_seed: str | None = None
                           ) -> tuple[BaseModel | None, WorkflowError | None]:
        """组装式严格校验：模型输出语义字段，workflow 补充 artifact_id/request_id/
        participant_refs/hashes，再 model_validate。

        input_fingerprint 绑定本节点真实输入（非 64 个 0）；content_hash 对语义内容
        做规范 hash。任一必需语义字段缺失、额外字段、普通文本、任意 dict 一律
        SCHEMA_VALIDATION_FAILED。
        """
        if not policy.output_artifact_type:
            return None, None
        if not isinstance(parsed, dict):
            return None, WorkflowError("SCHEMA_VALIDATION_FAILED",
                                       f"模型输出不符合 {policy.output_artifact_type} Schema")
        fingerprint = canonical_json_hash(
            {"request_id": request_id, "participant_refs": list(participant_refs),
             "seed": fingerprint_seed or ""})
        for name in (t.strip() for t in policy.output_artifact_type.split("|")):
            cls = ARTIFACT_TYPES.get(name)
            if cls is None:
                return None, WorkflowError("SCHEMA_VALIDATION_FAILED", f"未知 Artifact 类型: {name}")
            assembled = dict(parsed)
            # workflow 管理字段：无条件覆盖为权威值，模型提交值一律不采用
            assembled["artifact_id"] = str(uuid.uuid4())
            assembled["request_id"] = str(request_id)
            if "participant_refs" in cls.model_fields:
                assembled["participant_refs"] = list(participant_refs)
            if "input_fingerprint" in cls.model_fields:
                assembled["input_fingerprint"] = fingerprint
            if "content_hash" in cls.model_fields:
                assembled["content_hash"] = "0" * 64  # 之后由 _content_hash 重算
            try:
                artifact = cls.model_validate(assembled)
            except ValidationError:
                continue
            artifact = artifact.model_copy(
                update={"content_hash": self._content_hash(artifact)})
            return artifact, None
        return None, WorkflowError("SCHEMA_VALIDATION_FAILED",
                                   f"模型输出不符合 {policy.output_artifact_type} Schema")

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
    def _extract_final_validation(tool_ctx: ToolContext) -> FinalValidationArtifact | None:
        """FinalValidationArtifact 必须来自 B4 权威产出（tool_handler 已构建可引用 Artifact）。"""
        fv = tool_ctx.previous_results.get("final_validation")
        if not isinstance(fv, FinalValidationArtifact):
            return None
        return fv

    @staticmethod
    def _decision_refs_error(md: MenuDecisionArtifact,
                             fv: FinalValidationArtifact | None,
                             feasible_artifact: FeasibleMenuArtifact | None) -> WorkflowError | None:
        """引用生命周期：决策必须引用 B4 FinalValidationArtifact 与 FeasibleMenuArtifact 的实际 id。"""
        if fv is None or md.final_validation_ref != str(fv.artifact_id):
            return WorkflowError("ARTIFACT_INTEGRITY_FAILED",
                                 "MenuDecisionArtifact 引用的 final_validation_ref 不一致")
        if feasible_artifact is None or md.feasible_menu_artifact_ref != str(feasible_artifact.artifact_id):
            return WorkflowError("ARTIFACT_INTEGRITY_FAILED",
                                 "MenuDecisionArtifact 引用的 feasible_menu_artifact_ref 不一致")
        return None

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
                     user_input: str, tool_ctx: ToolContext,
                     response_format: dict | None = None,
                     already_executed: set[str] | None = None,
                     deterministic_results: list[dict] | None = None) -> dict:
        """调用 LLM——函数调用模式 + 严格 json_schema（可选）。

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
        used_tools = set(already_executed or ())
        if deterministic_results:
            rendered = "\n".join(
                f"[{item['tool']}] "
                f"{json.dumps(item['result'], ensure_ascii=False, default=str)[:1000]}"
                for item in deterministic_results
            )
            full_user += f"\n\n## 已执行确定性工具\n{rendered}"

        for _round in range(5):
            remaining_defs = [
                item for item in (tool_defs or [])
                if item["function"]["name"] not in used_tools
            ]
            try:
                response = self._llm.invoke(
                    role, system_prompt, full_user,
                    tools=remaining_defs or None,
                    response_format=response_format,
                )
            except Exception as e:
                return {"status": "failed", "error": str(e), "content": ""}

            if response.get("_mock"):
                return {"status": "failed", "error": "MODEL_MOCK_RESPONSE", "content": ""}

            tool_calls = response.get("tool_calls", [])
            content = response.get("content", "")

            if tool_calls:
                tool_results = []
                allowed_names = {t.name for t in policy.allowed_tools}
                terminal = None
                for tc in tool_calls:
                    name = tc.get("name", "")
                    args = tc.get("arguments", {})
                    if name not in allowed_names:
                        tool_results.append({"tool": name, "error": "TOOL_PERMISSION_DENIED"})
                        continue
                    if name in used_tools:
                        tool_results.append({"tool": name, "error": "TOOL_ALREADY_EXECUTED"})
                        continue
                    result = tool_handler.execute(name, args)
                    used_tools.add(name)
                    # MC-01-R2 P0-2：generate_feasible_menus 返回业务终态 → 立即停止
                    # 同批 tool_calls 循环；其后的 expand_retrieval/adjust_menu_plan
                    # 等工具绝不执行；已执行回执仍进入统一校验。
                    if name == "generate_feasible_menus" and isinstance(result, dict):
                        note = result.get("note")
                        if note in ("no_safe_menu", "no_feasible_menu"):
                            terminal = note
                            tool_results.append({"tool": name, "result": result})
                            break
                    tool_results.append({"tool": name, "result": result})

                if terminal is not None:
                    return {"status": "terminal", "terminal": terminal, "content": ""}

                results_text = "\n".join(
                    f"[{tr['tool']}] {json.dumps(tr.get('result', tr.get('error', '')), ensure_ascii=False, default=str)[:500]}"
                    for tr in tool_results
                )
                full_user += f"\n\n## 工具执行结果\n{results_text}"
                continue

            content_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if not content_text.strip():
                return {"status": "failed", "error": "MODEL_EMPTY_OUTPUT", "content": ""}

            return self._parse_result(content, role)

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
        """解析模型输出为 dict。"""
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
        """构建回答节点的公开事实基座：真实菜名 + 唯一 menu_hash/引用 + 时间说明。"""
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
            "plan_id": plan_id,
            "recipe_ids": recipe_ids,
            "menu_hash": md.menu_hash if md else menu_hash_for(plan_id, recipe_ids),
            "menu_ref": md.feasible_menu_artifact_ref if md else "",
            "final_validation_ref": str(final_artifact.artifact_id) if final_artifact else "",
            "final_validation_status": final_artifact.status if final_artifact else "",
            "participant_refs": list(md.participant_refs) if md else [],
            "request_id": str(md.request_id) if md else "",
            "time_data": _time_data,
            "requested_time_limit_minutes": tool_ctx.time_limit_minutes,
        }

    @staticmethod
    def _coverage_refs(fva: FinalValidationArtifact | None) -> list[str]:
        """健康关系/覆盖引用：顶层 relation_evidence_refs + 每菜品 participant 证据。"""
        if not isinstance(fva, FinalValidationArtifact):
            return []
        refs = list(fva.relation_evidence_refs)
        for pr in fva.participant_recipe_results:
            refs.extend(pr.constraint_refs)
            refs.extend(pr.ingredient_set_evidence_paths)
            refs.extend(pr.coverage_refs)
        return refs

    @staticmethod
    def _artifact_answer_text(a: AnswerArtifact) -> str:
        """从正式 AnswerArtifact.content 提取用户可见回答文本。"""
        c = a.content
        parts = [c.conclusion, c.menu_summary, c.reasoning_summary,
                 c.health_note, c.time_note]
        return "\n".join(str(p) for p in parts if p and str(p).strip()).strip()

    @staticmethod
    def _committed_result_summary(
        fva: FinalValidationArtifact,
        ans: AnswerArtifact,
        build_id: str,
        menu_items: list[dict] | None = None,
    ) -> dict:
        """构造已提交结果的公开只读投影（API 轮询/刷新恢复使用）。

        值只来自已通过最终校验并进入 Application 提交的 Artifact；不得从模型文本
        解析菜品身份，也不得从 SSE/Redis 反推提交事实。
        """
        return {
            "status": RequestStatus.COMPLETED.value,
            "answer": {
                "text": WorkflowRunner._artifact_answer_text(ans),
                "menu_ref": ans.menu_ref,
                "evidence_refs": list(ans.evidence_refs),
            },
            "menu_summary": {
                "build_id": build_id,
                "plan_id": fva.plan_id,
                "menu_hash": fva.menu_hash,
                "recipe_ids": list(fva.recipe_ids),
                "items": list(menu_items or []),
            },
        }

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
                  c4: ContextService, lock_token: str | None = None) -> None:
        """终态提交。不修改 state（runner 已保证终态）。

        fencing token 端到端传递到会话提交（T19 将作为 MySQL 提交强制参数）。
        """
        effective_status = state.status.value
        error = state.error
        result_summary: dict = {"status": effective_status}

        if effective_status == "completed":
            try:
                from food_agent_v2.application import commit_request_result
                fva = state.final_validation_artifact
                mda = state.menu_decision_artifact
                rva = state.review_artifact
                ans = state.answer_artifact
                hea = state.health_evaluation_artifact
                rid_text = str(fva.request_id) if isinstance(fva, FinalValidationArtifact) else request_id
                health_evidence = {
                    "request_id": request_id,
                    "build_id": state.build_id,
                    "plan_id": fva.plan_id if isinstance(fva, FinalValidationArtifact) else "",
                    "recipe_ids": list(fva.recipe_ids) if isinstance(fva, FinalValidationArtifact) else [],
                    "menu_hash": fva.menu_hash if isinstance(fva, FinalValidationArtifact) else "",
                    "final_validation": {
                        "ref": str(fva.artifact_id) if isinstance(fva, FinalValidationArtifact) else "",
                        "request_id": rid_text,
                        "plan_id": fva.plan_id if isinstance(fva, FinalValidationArtifact) else "",
                        "menu_hash": fva.menu_hash if isinstance(fva, FinalValidationArtifact) else "",
                        "recipe_ids": list(fva.recipe_ids) if isinstance(fva, FinalValidationArtifact) else [],
                        "verdict": fva.status if isinstance(fva, FinalValidationArtifact) else "",
                        "input_fingerprint": fva.input_fingerprint
                        if isinstance(fva, FinalValidationArtifact) else "",
                    },
                    "menu_decision": {
                        "ref": str(mda.artifact_id) if isinstance(mda, MenuDecisionArtifact) else "",
                        "request_id": str(mda.request_id) if isinstance(mda, MenuDecisionArtifact) else rid_text,
                        "plan_id": mda.plan_id if isinstance(mda, MenuDecisionArtifact) else "",
                        "menu_hash": mda.menu_hash if isinstance(mda, MenuDecisionArtifact) else "",
                        "content_hash": mda.content_hash if isinstance(mda, MenuDecisionArtifact) else "",
                    },
                    "review": {
                        "ref": str(rva.artifact_id) if isinstance(rva, ReviewArtifact) else "",
                        "request_id": str(rva.request_id) if isinstance(rva, ReviewArtifact) else rid_text,
                        "status": rva.status if isinstance(rva, ReviewArtifact) else "",
                        "content_hash": rva.content_hash if isinstance(rva, ReviewArtifact) else "",
                    },
                    "answer": {
                        "ref": str(ans.artifact_id) if isinstance(ans, AnswerArtifact) else "",
                        "request_id": str(ans.request_id) if isinstance(ans, AnswerArtifact) else rid_text,
                        "plan_id": ans.plan_id if isinstance(ans, AnswerArtifact) else "",
                        "menu_hash": ans.menu_hash if isinstance(ans, AnswerArtifact) else "",
                        "recipe_ids": list(ans.recipe_ids) if isinstance(ans, AnswerArtifact) else [],
                        "content_hash": ans.content_hash if isinstance(ans, AnswerArtifact) else "",
                        "content": {
                            "conclusion": ans.content.conclusion if isinstance(ans, AnswerArtifact) else "",
                            "menu_summary": ans.content.menu_summary if isinstance(ans, AnswerArtifact) else "",
                        },
                    },
                    "participant_constraint_refs": list(fva.participant_refs)
                    if isinstance(fva, FinalValidationArtifact) else [],
                    "ingredient_relation_coverage_refs": WorkflowRunner._coverage_refs(fva),
                    "override_refs": [],  # INV-016：永久约束覆盖请求一律拒绝 → 无 override
                    # 排除输出嵌入随机身份的工具（final_validation_ref/tool_call_id），
                    # 使审计信封确定性可幂等；其确定性本质由 final_validation 块覆盖。
                    "tool_receipt_refs": [
                        r.tool_call_id for r in state.tool_receipts
                        if r.tool_name not in WorkflowRunner.NONDETERMINISTIC_OUTPUT_TOOLS],
                    "tool_input_output_hashes": [
                        {"tool_call_id": r.tool_call_id,
                         "input_hash": r.input_hash,
                         "output_hash": r.output_hash}
                        for r in state.tool_receipts
                        if r.tool_name not in WorkflowRunner.NONDETERMINISTIC_OUTPUT_TOOLS],
                    "constraint_set_refs": list(hea.constraint_set_refs)
                    if isinstance(hea, HealthEvaluationArtifact) else [],
                    "final_validation_verdict": fva.status
                    if isinstance(fva, FinalValidationArtifact) else "",
                    "review_verdict": rva.status if isinstance(rva, ReviewArtifact) else "",
                    "answer_nonempty": bool(ans.content.conclusion.strip())
                    if isinstance(ans, AnswerArtifact) else False,
                    "tool_receipt_count": len(state.tool_receipts),
                }
                # 结构化菜单名只从同一 ready build 的固定 B3 视图派生；
                # 不解析模型回答，不允许缺菜/错 build 后继续提交。
                from food_agent_v2.application.menu_projection import build_public_menu
                menu_items = build_public_menu(
                    list(fva.recipe_ids) if isinstance(fva, FinalValidationArtifact) else [],
                    state.build_id,
                )
                health_evidence["menu_items"] = menu_items
                commit_request_result(
                    request_id=request_id,
                    session_id=state.shared_context_ref or "",
                    status="completed",
                    final_plan_id=fva.plan_id if isinstance(fva, FinalValidationArtifact) else "",
                    health_evidence=health_evidence,
                    participant_refs=state.participant_refs,
                    fencing_token=lock_token,
                    answer_text=WorkflowRunner._artifact_answer_text(ans)
                    if isinstance(ans, AnswerArtifact) else "",
                    menu_hash=fva.menu_hash if isinstance(fva, FinalValidationArtifact) else "",
                    menu_ref=ans.menu_ref if isinstance(ans, AnswerArtifact) else "",
                    evidence_refs=list(ans.evidence_refs)
                    if isinstance(ans, AnswerArtifact) else [],
                )
                # 到这里 Application 的原子事务已经提交，业务结果正式 completed。
                # D1 只保存同一提交事实的公开投影，供 SSE 中断后的轮询/刷新恢复。
                if (isinstance(fva, FinalValidationArtifact)
                        and isinstance(ans, AnswerArtifact)):
                    result_summary = self._committed_result_summary(
                        fva, ans, state.build_id, menu_items)
            except Exception as e:  # noqa: BLE001
                effective_status = RequestStatus.FAILED.value
                error = error or WorkflowError("AUDIT_COMMIT_FAILED", str(e))
                result_summary = {"status": effective_status}

            if effective_status == RequestStatus.COMPLETED.value:
                # outbox 是提交后的传输层。即时投递异常不得否定已经提交的 MySQL
                # 业务事实；pending/dispatching 记录由后续 dispatcher 重试。
                try:
                    from food_agent_v2.application.outbox import dispatch_request
                    dispatch_request(request_id)
                except Exception:
                    pass

        d1_api.update_status(request_id, effective_status,
                            result_summary=result_summary,
                            error={"code": error.error_code,
                                   "message": error.message}
                            if error else None)
        # 业务终态 SSE 通知：completed 仍由 answer_ready + result_committed
        # outbox 完成；cancelled 由 cancel_request 的 request_cancelled 保持；
        # 其余业务终态分别发布（绝不伪装成普通 failed）。
        _terminal_notice = {
            "no_safe_menu", "no_feasible_menu", "strict_time_indeterminate",
            "failed", "interrupted",
        }
        if effective_status in _terminal_notice:
            d1_api.publish_terminal(
                request_id, effective_status,
                message=error.message if error else None,
            )
        elif effective_status == "needs_clarification":
            d1_api.publish_clarification_event(request_id)
        commit_c4 = getattr(c4, "commit_session_state", None)
        if commit_c4 is not None:
            import inspect as _inspect

            if "token" in _inspect.signature(commit_c4).parameters:
                commit_c4(request_id, effective_status, token=lock_token)
            else:
                commit_c4(request_id, effective_status)
