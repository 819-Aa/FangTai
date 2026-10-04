"""LangGraph Agent 共用运行时：锁、心跳、证据构建与原子提交。

编排图在 ``graph_orchestrator`` 中定义；这里不选择业务行动或执行线性主链。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from food_agent_v2.c3 import WorkflowError
from food_agent_v2.c3.llm_client import LLMClient, get_llm_client
from food_agent_v2.c3.perf import PerfTrace
from food_agent_v2.c3.state import (
    NodeType,
    RequestStatus,
    WorkflowState,
    reduce_workflow_state,
)
from food_agent_v2.c3.tool_handler import ToolContext
from food_agent_v2.c4 import (
    ContextService,
)
from food_agent_v2.contracts.artifacts import (
    AnswerArtifact,
    FeasibleMenu,
    FeasibleMenuArtifact,
    FinalValidationArtifact,
    HealthEvaluationArtifact,
    MenuDecisionArtifact,
    MenuScoreDecomposition,
    ParticipantRecipeHealthResult,
    QueryPlanArtifact,
    ReviewArtifact,
)
from food_agent_v2.contracts.build import canonical_json_hash
from food_agent_v2.contracts.receipts import ToolReceipt
from food_agent_v2.d1 import api as d1_api

logger = logging.getLogger(__name__)

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


class AgentRuntime:
    """有界状态机执行器：build_id/node_id 注入 + 纯 reducer + 严格 Artifact 契约。
    LangGraph 编排器复用锁、心跳、结构化校验和原子提交能力。
    """

    #: 会话锁 TTL（秒）；heartbeat 按 LOCK_TTL/3 续租，保证单个模型调用不会失锁。
    LOCK_TTL = 30
    EXECUTION_LEASE_SECONDS = 60
    EXECUTION_MAX_SECONDS = 600

    #: 输出嵌入随机身份（UUID ref / tool_call_id）的工具，其 output_hash 非确定性，
    #: 不进入健康审计信封（确定性证据见 _canonical_audit）。
    NONDETERMINISTIC_OUTPUT_TOOLS = {
        "validate_selected_menu_health",
        "get_execution_trace",
        "get_artifact_chain",
    }

    QUERY_PLAN_SNAPSHOT_FIELDS = (
        "rewritten_query",
        "meal_types",
        "population_tags",
        "dish_types",
        "taste_tags",
        "cuisine_tags",
        "scenario_tags",
        "include_ingredients",
        "exclude_ingredients",
        "nutrition_goal_codes",
        "dish_count_requested",
        "health_exclusions",
        "time_constraint_seconds",
        "time_constraint_policy",
    )

    @classmethod
    def _query_plan_snapshot(cls, artifact: Any) -> dict | None:
        """只投影可供下一轮复用的稳定语义字段。"""
        if isinstance(artifact, BaseModel):
            data = artifact.model_dump(mode="json")
        elif isinstance(artifact, dict):
            data = artifact
        else:
            return None
        snapshot = {}
        for field in cls.QUERY_PLAN_SNAPSHOT_FIELDS:
            value = data.get(field)
            snapshot[field] = list(value) if isinstance(value, tuple) else value
        return snapshot

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
        self._trace: PerfTrace | None = None
        # P0.2：成功 outbox 投递延迟到会话锁释放后（result_committed 不得早于锁释放）
        self._pending_dispatch_request_id: str | None = None

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

    @staticmethod
    def _register_active_fencing_token(c4: ContextService, session_id: str, token: int) -> bool:
        register = getattr(c4, "register_active_fencing_token", None)
        if register is None:
            return True  # 测试 Fake 无持久化记忆支持
        return bool(register(session_id, token))

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
                        interval: float, deadline: float | None = None) -> None:
        while not stop.is_set() and not lost.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                lost.set()
                return
            try:
                if not renew(session_id, token):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return
            wait_seconds = interval
            if deadline is not None:
                wait_seconds = max(0.0, min(interval, deadline - time.monotonic()))
            stop.wait(wait_seconds)

    def _start_execution_heartbeat(
        self, c4: ContextService, request_id: str,
        owner: str | None, generation: int | None,
        lost: threading.Event, stop: threading.Event,
    ):
        if owner is None:
            return None
        renew = getattr(c4, "renew_request_execution_v2", None)
        if generation is None or renew is None:
            lost.set()
            return None
        interval = max(0.2, self.EXECUTION_LEASE_SECONDS / 3.0)
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(
                lambda _rid, _owner: renew(
                    request_id, owner, generation, self.EXECUTION_LEASE_SECONDS,
                ),
                request_id, owner, lost, stop, interval,
                time.monotonic() + self.EXECUTION_MAX_SECONDS,
            ),
            daemon=True,
            name=f"c3-execution-heartbeat-{request_id}",
        )
        thread.start()
        return thread

    @staticmethod
    def _stop_heartbeat(heartbeat, stop: threading.Event) -> None:
        if heartbeat is None:
            return
        stop.set()
        heartbeat.join(timeout=5)

    def _finalize_lock_failure(self, request_id: str, c4: ContextService) -> None:
        """锁不可用/被占用 → fail-closed 终态（不伪造放行）。"""
        if getattr(self, "_execution_owner", None) is not None:
            # v2 的新代次可能在旧代次 Redis 会话锁到期前被重领。
            # 留给相同 POST 再次恢复，不把锁竞争写成权威 failed。
            d1_api.update_status(request_id, "recovery_required", error={
                "code": "SESSION_LOCK_UNAVAILABLE",
                "message": "会话锁仍被占用，请稍后重试原请求",
            })
            return
        state = WorkflowState(request_id=request_id, status=RequestStatus.FAILED)
        state.error = WorkflowError(
            "SESSION_LOCK_UNAVAILABLE",
            "会话锁不可用或被占用（Redis 不可用或并发请求）",
            failed_node=NodeType.CONTEXT_BUILDING)
        self._finalize(state, request_id, c4)

    def run(self, request_id: str, session_id: str,
            message: str, participants: list[dict],
            config: dict | None = None,
            clarification_response: dict | None = None,
            execution_owner: str | None = None,
            execution_generation: int | None = None,
            **extra_kwargs: Any) -> None:
        """执行完整有界状态机（会话锁覆盖同一 session 的读取/运行/提交）。"""
        self._trace = PerfTrace(request_id=request_id)
        self._session_id = session_id
        self._execution_owner = execution_owner
        self._execution_generation = execution_generation
        self._execution_lost = None
        c4 = self._get_c4()
        lock_token = self._acquire_session_lock(c4, session_id)
        if lock_token is None:
            # Redis 不可用/锁被占用 → fail-closed（不伪造 token 放行）
            self._finalize_lock_failure(request_id, c4)
            return
        # 新 token 取得后，在启动 Agent 前以 FOR UPDATE 比较并登记 sessions.active_fencing_token
        try:
            token_int = int(lock_token)
        except (ValueError, TypeError):
            token_int = None
        if token_int is not None:
            registered = False
            try:
                registered = self._register_active_fencing_token(c4, session_id, token_int)
            except Exception as e:
                logger.error("登记 active_fencing_token 异常: %s", e)
                registered = False
            if not registered:
                # token 不大于已登记值或数据库异常 → 释放 Redis 锁并 fail-closed
                self._release_session_lock(c4, session_id, lock_token)
                self._finalize_lock_failure(request_id, c4)
                return
        # 绑定锁到 C4：工具持久化/最终提交失锁即 fail-closed
        self._bind_session_lock(c4, session_id, lock_token)
        lost = threading.Event()
        self._execution_lost = lost if execution_owner is not None else None
        stop = threading.Event()
        heartbeat = self._start_heartbeat(c4, session_id, lock_token, lost, stop)
        execution_heartbeat = self._start_execution_heartbeat(
            c4, request_id, execution_owner, execution_generation, lost, stop,
        )
        try:
            self._run_locked(request_id, session_id, message, participants,
                             config, c4, lock_token, lost,
                             clarification_response=clarification_response,
                             **extra_kwargs)
        finally:
            # 先停并 join heartbeat，再原子释放（避免续租与释放竞态）
            self._stop_heartbeat(heartbeat, stop)
            self._stop_heartbeat(execution_heartbeat, stop)
            self._unbind_session_lock(c4, session_id)
            self._release_session_lock(c4, session_id, lock_token)
            # 锁释放后再投递成功 outbox（result_committed 不得早于锁释放）
            pending = getattr(self, "_pending_dispatch_request_id", None)
            if pending is not None:
                try:
                    from food_agent_v2.application.outbox import dispatch_request
                    dispatch_request(pending)
                except Exception:
                    pass


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
                    estimated_time_feasible=p.estimated_time_feasible,
                    estimated_makespan_seconds=p.estimated_makespan_seconds,
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

    def _handle_query_plan_exclusions(
        self,
        state: WorkflowState,
        artifact: QueryPlanArtifact,
        session_id: str,
        c4: ContextService,
        user_id_mapping: dict[str, int],
    ) -> WorkflowState:
        """R-002：消费 QueryPlanArtifact.health_exclusions，建立临时健康约束闭环。

        每个排除项：parse → b2.validate_temporary_signal（fail-closed）→
        c4.store_temporary_constraint。任一无法封闭映射（HEALTH_SIGNAL_AMBIGUOUS）
        → 返回 needs_clarification 状态；全部成功返回原 state（继续主流程）。

        契约：**始终返回 state，绝不返回 None**（调用方直接 `state = ...` 后
        立即 `state.is_terminal()`，None 会导致 AttributeError）。
        """
        from food_agent_v2.b2 import HealthProfileError, UserHealthProfileService

        exclusions = getattr(artifact, "health_exclusions", ()) or ()
        if not exclusions:
            return state

        b2 = UserHealthProfileService()
        b2.load(expected_build_id=state.build_id)
        # B3 食材解析器（禁忌信号绑定标准 ingredient_id）
        resolver = _ingredient_resolver()

        for raw in exclusions:
            try:
                parsed = _parse_health_exclusion(raw)
            except ValueError:
                return self._needs_clarification(
                    state, "HEALTH_SIGNAL_AMBIGUOUS", f"无法解析健康排除项: {raw!r}")
            ref = parsed["participant_ref"]
            # 归属校验：排除项参与者必须在本请求参与者内
            if ref not in user_id_mapping:
                return self._needs_clarification(
                    state, "HEALTH_SIGNAL_AMBIGUOUS", f"排除项参与者不在请求内: {ref}")
            try:
                temp = b2.validate_temporary_signal(
                    {"type": parsed["type"], "value": parsed["value"]},
                    ref,
                    ingredient_resolver=resolver,
                )
            except HealthProfileError as e:
                return self._needs_clarification(state, e.code, str(e))
            c4.store_temporary_constraint(session_id, {
                "constraint_code": temp.constraint_code,
                "taboo_ingredient_name": temp.taboo_ingredient_name,
                "taboo_ingredient_id": temp.taboo_ingredient_id,
                "participant_ref": temp.participant_ref,
                "source_refs": list(temp.source_refs or []),
                "scope": temp.scope.value,
            })
        return state

    @staticmethod
    def _needs_clarification(
        state: WorkflowState, code: str, message: str,
    ) -> WorkflowState:
        """基于当前 state 构造 needs_clarification 终态（query_understanding 分支）。"""
        from food_agent_v2.c3.state import WorkflowError, reduce_workflow_state
        new_state = reduce_workflow_state(
            state, action="query_understanding", needs_clarification=True)
        new_state.error = WorkflowError(code, message)
        return new_state







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








    # ---- 提取与构建辅助 ----


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
                "text": AgentRuntime._artifact_answer_text(ans),
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
            return RedisSessionStore().is_request_cancelled(request_id)
        except Exception:
            return False

    def _finalize(self, state: WorkflowState, request_id: str,
                  c4: ContextService, lock_token: str | None = None,
                  clarification_question_id: str | None = None,
                  clarification_transition: Any | None = None,
                  execution_owner: str | None = None,
                  execution_generation: int | None = None) -> str:
        """终态提交。不修改 state（runner 已保证终态）。

        fencing token 端到端传递到会话提交（T19 将作为 MySQL 提交强制参数）。
        """
        effective_status = state.status.value
        error = state.error
        result_summary: dict = {"status": effective_status}
        durable_committed = False
        execution_owner = execution_owner or getattr(self, "_execution_owner", None)
        if execution_generation is None:
            execution_generation = getattr(self, "_execution_generation", None)
        v2_execution = execution_owner is not None
        if v2_execution and getattr(self, "_execution_lost", None) is not None:
            if self._execution_lost.is_set():
                return "recovery_required"
        commit_execution = (
            {"execution_owner": execution_owner,
             "execution_generation": execution_generation}
            if v2_execution else {}
        )

        if v2_execution:
            renew = getattr(c4, "renew_request_execution_v2", None)
            try:
                lease_held = bool(
                    renew(request_id, execution_owner, execution_generation,
                          self.EXECUTION_LEASE_SECONDS)
                ) if renew is not None and execution_generation is not None else False
            except Exception:
                lease_held = False
            if not lease_held:
                return "recovery_required"  # 旧代次不得投影任何终态

        # 失锁前置检查：提交前再次确认 Redis 锁归属（fail-closed）
        if lock_token and (v2_execution or effective_status in {
            "completed", "needs_clarification",
        }) and not self._session_lock_held(
            c4, getattr(self, "_session_id", "") or state.shared_context_ref or "", lock_token,
        ):
            if v2_execution:
                return "recovery_required"
            effective_status = RequestStatus.FAILED.value
            error = WorkflowError("SESSION_LOCK_LOST", "提交前检测到会话锁已失效")
            result_summary = {"status": effective_status}

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
                    "ingredient_relation_coverage_refs": AgentRuntime._coverage_refs(fva),
                    "override_refs": [],  # INV-016：永久约束覆盖请求一律拒绝 → 无 override
                    # 排除输出嵌入随机身份的工具（final_validation_ref/tool_call_id），
                    # 使审计信封确定性可幂等；其确定性本质由 final_validation 块覆盖。
                    "tool_receipt_refs": [
                        r.tool_call_id for r in state.tool_receipts
                        if r.tool_name not in AgentRuntime.NONDETERMINISTIC_OUTPUT_TOOLS],
                    "tool_input_output_hashes": [
                        {"tool_call_id": r.tool_call_id,
                         "input_hash": r.input_hash,
                         "output_hash": r.output_hash}
                        for r in state.tool_receipts
                        if r.tool_name not in AgentRuntime.NONDETERMINISTIC_OUTPUT_TOOLS],
                    "constraint_set_refs": list(hea.constraint_set_refs)
                    if isinstance(hea, HealthEvaluationArtifact) else [],
                    "final_validation_verdict": fva.status
                    if isinstance(fva, FinalValidationArtifact) else "",
                    "review_verdict": rva.status if isinstance(rva, ReviewArtifact) else "",
                    "answer_nonempty": bool(ans.content.conclusion.strip())
                    if isinstance(ans, AnswerArtifact) else False,
                    "tool_receipt_count": len(state.tool_receipts),
                }
                query_plan = self._query_plan_snapshot(state.query_plan_artifact)
                if query_plan is not None:
                    health_evidence["query_plan"] = query_plan
                if clarification_question_id:
                    health_evidence["accepted_clarification_question_id"] = clarification_question_id
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
                    answer_text=AgentRuntime._artifact_answer_text(ans)
                    if isinstance(ans, AnswerArtifact) else "",
                    menu_hash=fva.menu_hash if isinstance(fva, FinalValidationArtifact) else "",
                    menu_ref=ans.menu_ref if isinstance(ans, AnswerArtifact) else "",
                    evidence_refs=list(ans.evidence_refs)
                    if isinstance(ans, AnswerArtifact) else [],
                    clarification_transition=clarification_transition,
                    **commit_execution,
                )
                durable_committed = True
                # 到这里 Application 的原子事务已经提交，业务结果正式 completed。
                # D1 只保存同一提交事实的公开投影，供 SSE 中断后的轮询/刷新恢复。
                if (isinstance(fva, FinalValidationArtifact)
                        and isinstance(ans, AnswerArtifact)):
                    result_summary = self._committed_result_summary(
                        fva, ans, state.build_id, menu_items)
                # 提交成功后才更新 current_menu：失败/取消绝不遗留幻影菜单（C4-02 闭环）。
                c4_sessions = getattr(c4, "_sessions", None)
                c4_ctx = c4_sessions.get(state.shared_context_ref or "") if c4_sessions else None
                if c4_ctx is not None and isinstance(fva, FinalValidationArtifact):
                    c4_ctx.current_menu.plan_id = fva.plan_id
                    c4_ctx.current_menu.recipe_ids = list(fva.recipe_ids)
                    c4._recompute_manifest(c4_ctx)
                    c4._persist_session(c4_ctx)
            except Exception as e:  # noqa: BLE001
                if durable_committed:
                    logger.exception("已提交菜单的提交后投影失败: request_id=%s", request_id)
                else:
                    effective_status = "recovery_required" if v2_execution else RequestStatus.FAILED.value
                    error = error or WorkflowError("AUDIT_COMMIT_FAILED", str(e))
                    result_summary = {"status": effective_status}

            if effective_status == RequestStatus.COMPLETED.value:
                # 延迟到会话锁释放后再投递（P0.2）：避免 result_committed 先于锁释放，
                # 客户端收到提交事件立即发下一轮时拿到 SESSION_LOCK_UNAVAILABLE。
                self._pending_dispatch_request_id = request_id

        elif effective_status == "needs_clarification":
            # 无论是否有旧问题消费，只要进入 needs_clarification，
            # 均原子提交状态转移与新问题（或旧问题消费）。
            try:
                from food_agent_v2.application import commit_request_result

                commit_request_result(
                    request_id=request_id,
                    session_id=state.shared_context_ref or "",
                    status="needs_clarification",
                    final_plan_id="",
                    health_evidence={
                        "request_id": request_id,
                        "accepted_clarification_question_id": clarification_question_id,
                    },
                    participant_refs=state.participant_refs,
                    fencing_token=lock_token,
                    clarification_transition=clarification_transition,
                    **commit_execution,
                )
                durable_committed = True
                self._pending_dispatch_request_id = request_id
            except Exception as e:  # noqa: BLE001
                effective_status = "recovery_required" if v2_execution else RequestStatus.FAILED.value
                logger.exception("澄清状态提交失败: request_id=%s", request_id)
                error = WorkflowError("AUDIT_COMMIT_FAILED", str(e))
                result_summary = {"status": effective_status}

        elif v2_execution and effective_status in {
            "failed", "cancelled", "interrupted", "no_safe_menu", "no_feasible_menu",
        }:
            try:
                from food_agent_v2.application import commit_request_result

                commit_request_result(
                    request_id=request_id,
                    session_id=state.shared_context_ref or getattr(self, "_session_id", ""),
                    status=effective_status,
                    final_plan_id="",
                    health_evidence={"request_id": request_id},
                    participant_refs=state.participant_refs,
                    fencing_token=lock_token,
                    error_code=error.error_code if error else None,
                    error_message=error.message if error else None,
                    **commit_execution,
                )
                durable_committed = True
            except Exception as e:  # noqa: BLE001
                logger.exception("v2 错误终态提交失败: request_id=%s", request_id)
                effective_status = "recovery_required"
                error = WorkflowError("AUDIT_COMMIT_FAILED", str(e))
                result_summary = {"status": effective_status}

        if v2_execution and durable_committed:
            d1_api.mark_execution_committed(request_id, execution_owner, execution_generation)
        d1_api.update_status(request_id, effective_status,
                            result_summary=result_summary,
                            error={"code": error.error_code,
                                   "message": error.message}
                            if error else None)
        # 业务终态 SSE 通知：completed 仍由 answer_ready + result_committed
        # outbox 完成；needs_clarification 由 outbox 派送；cancelled 由 cancel_request 的 request_cancelled 保持；
        # 其余业务终态分别发布（绝不伪装成普通 failed）。
        _terminal_notice = {
            "no_safe_menu", "no_feasible_menu",
            "failed", "interrupted",
        }
        if effective_status in _terminal_notice:
            d1_api.publish_terminal(
                request_id, effective_status,
                message=error.message if error else None,
            )
        commit_c4 = getattr(c4, "commit_session_state", None)
        if commit_c4 is not None:
            import inspect as _inspect

            try:
                if "token" in _inspect.signature(commit_c4).parameters:
                    commit_c4(request_id, effective_status, token=lock_token)
                else:
                    commit_c4(request_id, effective_status)
            except Exception:
                if not durable_committed:
                    raise
                logger.exception("已提交请求的 C4 终态投影失败: request_id=%s", request_id)

        # P1：性能观测基线——终态后输出结构化耗时日志（纯观测，不改变行为）。
        trace = getattr(self, "_trace", None)
        if trace is not None:
            trace.terminal_at = time.perf_counter()
            print(trace.log_line())
        return effective_status


def _ingredient_resolver():
    """构造 B3 标准食材解析器（绑定 ready build）。

    用于临时禁忌信号 → 标准 ingredient_id。MySQL/Qdrant 不可用时返回 None，
    B2.validate_temporary_signal 会因无法解析而抛 HEALTH_SIGNAL_AMBIGUOUS
    → runner 进入 needs_clarification（fail-closed，不静默丢弃）。
    """
    try:
        from food_agent_v2.b3.identity_resolver import IngredientIdentityResolver
        return IngredientIdentityResolver()
    except Exception:
        return None


def _parse_health_exclusion(raw: str) -> dict:
    """解析 QueryPlanArtifact.health_exclusions 字符串（三前缀格式）。

    约定格式： "参与者N:过敏:值" | "参与者N:禁忌:值" | "参与者N:疾病:值"
    - 参与者前缀必须是 participant_ref（如 p1 / p2），否则无法归属 → 拒绝
    - 类型必须是 过敏/禁忌/疾病 之一
    - 值非空
    返回 {"type": ..., "value": ..., "participant_ref": ...}；
    任一不满足抛 ValueError（调用方转 needs_clarification）。
    """
    parts = raw.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"health_exclusion 格式应为 参与者N:类型:值: {raw!r}")
    participant_ref, type_text, value = parts
    if not participant_ref.startswith("p") or not value.strip():
        raise ValueError(f"health_exclusion 参与者或值无效: {raw!r}")
    type_map = {"过敏": "allergy", "禁忌": "taboo", "疾病": "disease"}
    signal_type = type_map.get(type_text)
    if signal_type is None:
        raise ValueError(f"health_exclusion 类型未知: {raw!r}")
    return {"type": signal_type, "value": value.strip(), "participant_ref": participant_ref}
