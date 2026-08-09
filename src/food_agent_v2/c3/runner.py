"""C3 工作流运行器 —— 执行状态机，串联五个模型。

模型通过函数调用自主选择工具，runner 执行真实领域服务并返回结果。
前后置校验在每个节点执行，工具回执写入 WorkflowState。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from food_agent_v2.c3 import (
    WorkflowState, RequestStatus, NodeType, WorkflowTransition,
    ROLE_POLICIES, RolePolicy, WorkflowError, NodeValidator, ToolReceipt,
)
from food_agent_v2.c3.prompts import get_prompt
from food_agent_v2.c3.llm_client import get_llm_client
from food_agent_v2.c3.tool_handler import ToolHandler, ToolContext
from food_agent_v2.c4 import ContextService, ConstraintScope
from food_agent_v2.d1 import api as d1_api


class WorkflowRunner:
    """工作流执行器。"""

    def __init__(self):
        self._llm = get_llm_client()
        self._c4: ContextService | None = None

    def _get_c4(self) -> ContextService:
        if self._c4 is None:
            self._c4 = ContextService()
        return self._c4

    def run(self, request_id: str, session_id: str,
            message: str, participants: list[dict],
            config: dict | None = None) -> None:
        """执行完整工作流。"""
        state = WorkflowState(request_id=request_id, status=RequestStatus.RUNNING)

        participant_refs = [p["participant_ref"] for p in participants]
        state.participant_refs = participant_refs
        user_id_mapping = {p["participant_ref"]: int(p["user_id"]) for p in participants}

        c4 = self._get_c4()

        # 工具上下文——整个请求共享（携带 c4 + session，供工具回写约束）
        tool_ctx = ToolContext(
            request_id=request_id,
            participant_user_mapping=user_id_mapping,
            session_id=session_id,
            context_service=c4,
        )

        # === 节点1: context_building ===
        state.current_node = NodeType.CONTEXT_BUILDING

        # INV-012：检测用户消息中的指令注入（不可信输入不能改变系统指令）
        from food_agent_v2.c3 import detect_untrusted_instruction
        _injection = detect_untrusted_instruction(message)
        if _injection:
            state.status = RequestStatus.FAILED
            state.error = WorkflowError(
                "UNTRUSTED_INSTRUCTION_DETECTED",
                f"检测到指令注入: {_injection}",
                failed_node=NodeType.CONTEXT_BUILDING,
            )
            self._finalize(state, request_id, c4); return

        ctx, manifest = c4.build_shared_context(
            session_id, participant_refs,
            {"raw_text": message, "timestamp": time.time()},
            user_id_mapping,
            request_id=request_id,
        )
        state.shared_context_ref = ctx.session_id

        # INV-009：校验上下文完整性（不可压缩核心块哈希）
        integrity = c4.validate_context_integrity(state.shared_context_ref)
        if not integrity.get("valid", False):
            state.status = RequestStatus.FAILED
            state.error = WorkflowError(
                "CONTEXT_INTEGRITY_FAILED",
                f"上下文完整性校验失败: {integrity.get('reason', 'core mismatch')}",
                failed_node=NodeType.CONTEXT_BUILDING,
            )
            self._finalize(state, request_id, c4); return

        d1_api.publish_analysis_event(
            request_id, "context_ready",
            f"已理解{len(participants)}位参与者的需求", []
        )

        # === 节点2: query_understanding ===
        state.current_node = NodeType.QUERY_UNDERSTANDING
        q_result = self._run_model_node(
            "query_understanding", state, c4, tool_ctx,
            user_message=message,
        )
        if state.is_terminal():
            self._finalize(state, request_id, c4); return

        # 查询理解认为缺少阻止安全执行的关键信息 → needs_clarification 终态（文档 09 §8.2）
        if isinstance(q_result, dict) and q_result.get("needs_clarification"):
            state.status = RequestStatus.NEEDS_CLARIFICATION
            d1_api.publish_clarification_event(request_id, q_result)
            self._finalize(state, request_id, c4); return

        state.query_plan_artifact = q_result
        d1_api.publish_analysis_event(request_id, "query_understanding",
                                      f"理解需求完成", [])

        # 提取初次检索的候选 recipe_ids，传递给健康规划节点（文档 09 §8.3：QueryPlan + RAG 候选进入该节点）
        retrieved_ids: list[int] = []
        _retrieval = tool_ctx.previous_results.get("retrieval")
        if _retrieval:
            retrieved_ids = [c.recipe_id for c in _retrieval.candidates]

        # 把查询理解提取的严格时间约束注入工具上下文（供 generate_feasible_menus 使用）
        try:
            tool_ctx.time_limit_minutes = (q_result or {}).get(
                "structured_requirements", {}).get("time_limit_minutes")
        except Exception:
            tool_ctx.time_limit_minutes = None

        # === 节点3+4: health_menu_planning + menu_decision（含最终健康校验回流） ===
        final_validation = None
        plan_id = ""
        recipe_ids: list[int] = []
        answer_text = ""
        review_result: dict = {}
        final_verdict = "PASS"

        while True:
            # --- 节点3: health_menu_planning ---
            state.current_node = NodeType.HEALTH_MENU_PLANNING
            hm_input = {
                "query_plan": state.query_plan_artifact,
                "retrieved_candidate_recipe_ids": retrieved_ids,
            }
            hm_result = self._run_model_node(
                "health_menu_planning", state, c4, tool_ctx,
                user_message=json.dumps(hm_input, ensure_ascii=False),
                handoff={"artifact_refs": ["query_plan", "retrieval"],
                         "action_required": "健康审查并生成菜单方案"},
            )
            if state.is_terminal():
                self._finalize(state, request_id, c4); return

            if not hm_result:
                state.status = RequestStatus.FAILED
                state.error = WorkflowError(
                    "HEALTH_MENU_PLANNING_EMPTY",
                    "健康与菜单规划节点未产生结果",
                    failed_node=NodeType.HEALTH_MENU_PLANNING,
                )
                self._finalize(state, request_id, c4); return

            state.health_evaluation_artifact = hm_result
            state.feasible_menu_artifact = hm_result
            d1_api.publish_analysis_event(request_id, "health_evaluation",
                                          "健康审查完成", [])
            d1_api.publish_analysis_event(request_id, "menu_planning",
                                          "菜单方案生成完成", [])

            # --- 节点4: menu_decision ---
            state.current_node = NodeType.MENU_DECISION
            md_pre_count = len(tool_ctx.tool_receipts)
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
            md_result = self._run_model_node(
                "menu_decision", state, c4, tool_ctx,
                user_message=json.dumps(md_input, ensure_ascii=False),
                handoff={"artifact_refs": ["health_eval", "feasible_menus"],
                         "action_required": "选择最优方案并最终校验"},
            )
            if state.is_terminal():
                self._finalize(state, request_id, c4); return

            state.menu_decision_artifact = md_result

            # 从本节点真实工具回执提取最终健康校验结果（文档 09 §8.4，INV-001）
            final_validation = self._extract_final_validation(
                tool_ctx.tool_receipts[md_pre_count:])

            plan_id, recipe_ids = self._extract_menu_decision(md_result or {})

            # 回退：模型可能调完校验工具却漏写最终 MenuDecisionArtifact JSON。
            # 若校验工具已成功（PASS）且携带 plan_id/recipe_ids，则采用该已校验方案
            # （基于 B4 真实回执，不是编造；文档 09 §8.4 允许 plan_id 来自已校验方案）。
            if (not plan_id or not recipe_ids) and final_validation and \
                    final_validation.get("verdict") == "PASS":
                fv_plan = final_validation.get("plan_id") or ""
                for r in tool_ctx.tool_receipts[md_pre_count:]:
                    if not isinstance(r, dict) or r.get("tool_name") != "validate_selected_menu_health":
                        continue
                    args = r.get("arguments_summary", {}) or {}
                    plan_id = plan_id or fv_plan
                    rids = args.get("recipe_ids", [])
                    if isinstance(rids, list):
                        recipe_ids = [int(x) for x in rids if str(x).isdigit()]
                    elif isinstance(rids, str):
                        import re as _re
                        recipe_ids = [int(x) for x in _re.findall(r"\d+", rids)]
                    break

            # 菜单决策必须产出有效菜单（防止空菜单一路跑到 completed）
            if not plan_id or not recipe_ids:
                state.status = RequestStatus.FAILED
                state.error = WorkflowError(
                    "MENU_DECISION_EMPTY",
                    "菜单决策未产生有效菜单（plan_id 或 recipe_ids 为空）",
                    failed_node=NodeType.MENU_DECISION,
                )
                self._finalize(state, request_id, c4); return

            # 最终健康校验 EXCLUDE → 有界重规划（文档 09 §13.2 / §8.4）
            if final_validation and final_validation.get("verdict") == "EXCLUDE":
                if state.check_and_increment("health_replan_count", 1):
                    state.status = RequestStatus.REVISING
                    continue  # 回流 health_menu_planning 重新规划
                state.status = RequestStatus.FAILED
                state.error = WorkflowError(
                    "FINAL_HEALTH_VALIDATION_FAILED",
                    "最终健康校验未通过且重规划已耗尽（最多 1 次）",
                    failed_node=NodeType.MENU_DECISION,
                )
                self._finalize(state, request_id, c4); return

            # 最终校验通过，退出重规划循环
            break

        # 写入 C4 current_menu（菜单历史、投影均依赖此字段）
        c4_ctx = c4._sessions.get(state.shared_context_ref or "")
        if c4_ctx:
            c4_ctx.current_menu.plan_id = plan_id
            c4_ctx.current_menu.recipe_ids = recipe_ids
            c4._persist_session(c4_ctx)

        d1_api.publish_analysis_event(request_id, "menu_decision",
                                      "菜单方案已选定", [])

        # 为回答模型注入最终菜单的公开事实（真实菜名，不能只给 recipe_id 数字）。
        # 文档 09 §8.5：回答基于最终菜单公开事实；B3 公开菜品视图随 HandoffMessage 进入回答节点。
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
        # 时间可用性：仅当菜单各菜都有 LLM 时间估算（llm_estimate）时才给具体分钟数；
        # 否则标记不可用，让回答模型如实说"暂无法提供"，不编造时间。
        if _makespan and _time_source == "llm_estimate":
            _time_data = {"total_minutes": max(1, round(_makespan / 60)),
                          "available": True, "source": "llm_estimate"}
        else:
            _time_data = {"available": False}
        _answer_base = {
            "selected_menu": {"plan_id": plan_id, "dishes": _dishes},
            "time_data": _time_data,
            "requested_time_limit_minutes": tool_ctx.time_limit_minutes,
        }

        # === 节点5-6: answer_generation + unified_review（含修订回路） ===
        revision_count = 0
        review_result = {}
        final_verdict = "PASS"

        while True:
            state.current_node = NodeType.ANSWER_GENERATION
            review_feedback = review_result.get("issue_list", []) if revision_count > 0 else []
            feedback_text = ""
            if review_feedback:
                feedback_text = f"\n\n## 上次审查反馈\n{json.dumps(review_feedback, ensure_ascii=False)}\n请修正后重新输出。"

            ans_result = self._run_model_node(
                "answer_generation", state, c4, tool_ctx,
                user_message=json.dumps(_answer_base, ensure_ascii=False) + feedback_text,
                handoff={"artifact_refs": ["menu_decision"],
                         "action_required": "生成用户可见回答"},
            )
            answer_text = self._extract_answer_text(ans_result)
            state.answer_artifact = ans_result

            # INV-005：确定性校验回答 dish_ids ⊆ 已校验菜单（不靠审查模型自觉）。
            # 模型输出 dish_ids 时精确校验；未输出时由统一审查模型兜底。
            answer_dish_ids = self._extract_answer_dish_ids(ans_result)
            if answer_dish_ids:
                if not set(answer_dish_ids).issubset(set(recipe_ids)):
                    state.status = RequestStatus.FAILED
                    state.error = WorkflowError(
                        "ANSWER_GROUNDING_FAILED",
                        f"回答引用非菜单菜品: {answer_dish_ids}",
                        failed_node=NodeType.ANSWER_GENERATION,
                    )
                    self._finalize(state, request_id, c4); return

            state.current_node = NodeType.UNIFIED_REVIEW
            review_result = self._run_model_node(
                "unified_review", state, c4, tool_ctx,
                user_message=answer_text,
                handoff={"artifact_refs": ["answer"],
                         "action_required": "审查回答是否符合规则"},
            )
            state.review_artifact = review_result

            verdict = review_result.get("verdict", "PASS")
            if verdict not in ("PASS", "REVISION_REQUIRED", "REVIEW_REQUIRED"):
                verdict = "PASS"

            if verdict == "PASS":
                final_verdict = "PASS"
                break
            elif verdict == "REVISION_REQUIRED":
                # 文档 09 §8.6：修订最多 1 次；超限 → WORKFLOW_RETRY_LIMIT_EXCEEDED → failed
                if revision_count >= 1:
                    final_verdict = "FAILED"
                    state.error = state.error or WorkflowError(
                        "WORKFLOW_RETRY_LIMIT_EXCEEDED",
                        "统一审查修订已达上限（最多 1 次）",
                        failed_node=NodeType.UNIFIED_REVIEW,
                    )
                    break
                revision_count += 1
                continue  # 回到 answer_generation 重新生成
            else:
                # REVIEW_REQUIRED / 未知：复审最多 1 次；超限 → failed
                if state.review_reevaluation_count >= 1:
                    final_verdict = "FAILED"
                    state.error = state.error or WorkflowError(
                        "WORKFLOW_RETRY_LIMIT_EXCEEDED",
                        "统一审查复审已达上限（最多 1 次）",
                        failed_node=NodeType.UNIFIED_REVIEW,
                    )
                    break
                state.review_reevaluation_count += 1
                continue

        # 修订循环结束——根据最终判定设置终态（文档 09 §20.5：不谎报 completed）
        if final_verdict == "PASS":
            if not (answer_text or "").strip():
                state.status = RequestStatus.FAILED
                state.error = WorkflowError(
                    "ANSWER_GROUNDING_FAILED",
                    "回答为空，禁止以空回答完成请求",
                    failed_node=NodeType.ANSWER_GENERATION,
                )
            else:
                state.status = RequestStatus.COMPLETED
        else:
            state.status = RequestStatus.FAILED
            state.error = state.error or WorkflowError(
                "ANSWER_REVIEW_FAILED",
                f"Review verdict after revision: {final_verdict}"
            )

        state.final_validation_artifact = {
            "verdict": (final_validation or {}).get("verdict", final_verdict),
            "plan_id": plan_id,
            "recipe_ids": recipe_ids,
            "review_details": review_result,
        }

        if state.status == RequestStatus.COMPLETED:
            d1_api.publish_answer_event(
                request_id, answer_text,
                state.answer_artifact.get("menu_ref", ""),
                state.answer_artifact.get("evidence_refs", [])
            )
            d1_api.publish_result_committed(
                request_id,
                {"menu": str(state.menu_decision_artifact)[:200],
                 "plan_id": plan_id}
            )

        # === 节点7: atomic_commit ===
        state.current_node = NodeType.ATOMIC_COMMIT
        WorkflowTransition.atomic_commit(state)
        self._finalize(state, request_id, c4)

    @staticmethod
    def _extract_menu_decision(result: dict) -> tuple[str, list[int]]:
        """从 menu_decision 模型输出中提取 (plan_id, recipe_ids)。

        兼容两种输出结构：
          - 顶层 {"plan_id":..., "recipe_ids":[...]}
          - 嵌套 {"MenuDecisionArtifact": {"selected_plan_id":..., "recipe_ids":[...]}}
        """
        plan_id = ""
        recipe_ids: list[int] = []
        candidates = [result]
        nested = result.get("MenuDecisionArtifact")
        if isinstance(nested, dict):
            candidates.append(nested)
        for c in candidates:
            if not isinstance(c, dict):
                continue
            pid = c.get("plan_id") or c.get("selected_plan_id")
            if pid:
                plan_id = str(pid)
            rids = c.get("recipe_ids") or []
            if rids:
                recipe_ids = [int(r) for r in rids if isinstance(r, int) or str(r).isdigit()]
        return plan_id, recipe_ids

    @staticmethod
    def _extract_answer_text(result: dict) -> str:
        """从回答模型输出中提取用户可见回答文本。

        兼容结构：
          - {"content": "<文本>"}
          - {"AnswerArtifact": {"conclusion":..., "menu_summary":..., ...}}
          - 顶层 conclusion/menu_summary 字段
        """
        if not isinstance(result, dict):
            return str(result or "")
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        art = result.get("AnswerArtifact")
        if isinstance(art, dict):
            result = art
        parts = [result.get(k) for k in
                 ("conclusion", "menu_summary", "reasoning_summary", "health_note", "time_note")]
        text = "\n".join(str(p) for p in parts if p and str(p).strip())
        return text.strip()

    @staticmethod
    def _extract_answer_dish_ids(result: dict) -> list[int]:
        """从回答模型输出中提取 dish_ids（兼容顶层 / AnswerArtifact 嵌套）。"""
        if not isinstance(result, dict):
            return []
        for cand in (result, result.get("AnswerArtifact")):
            if isinstance(cand, dict):
                ids = cand.get("dish_ids") or []
                if ids:
                    return [int(i) for i in ids if str(i).isdigit()]
        return []

    @staticmethod
    def _extract_final_validation(receipts: list[dict]) -> dict | None:
        """从 menu_decision 节点的工具回执中提取 validate_selected_menu_health 的真实结果。

        文档 09 §8.4：FinalValidationArtifact 必须来自 B4 工具回执，不能由工作流拼造。
        """
        for r in receipts:
            if not isinstance(r, dict):
                continue
            if r.get("tool_name") != "validate_selected_menu_health":
                continue
            summary = r.get("result_summary", "")
            try:
                data = json.loads(summary)
                if isinstance(data, dict) and "verdict" in data:
                    return {
                        "verdict": data.get("verdict"),
                        "plan_id": data.get("plan_id", ""),
                    }
            except (json.JSONDecodeError, TypeError):
                continue
        return None

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

    def _run_model_node(
        self, role: str, state: WorkflowState, c4: ContextService,
        tool_ctx: ToolContext, user_message: str,
        handoff: dict | None = None,
    ) -> dict:
        """执行一个模型节点：前置校验 → 模型调用 → 后置校验 → 返回结果。"""
        # 取消标记 → cancelled 终态（不进入模型调用）
        if self._is_cancelled(state.request_id):
            state.status = RequestStatus.CANCELLED
            return {}
        policy = ROLE_POLICIES[role]

        # 前置校验
        pre_err = NodeValidator.pre_check(state, policy)
        if pre_err:
            state.status = RequestStatus.FAILED
            state.error = pre_err
            return {}

        # 投影上下文
        model_ctx = c4.project_model_context(
            role, handoff, state.shared_context_ref or ""
        )

        # 记录本节点开始前的工具回执数量，用于隔离本节点的调用
        pre_count = len(tool_ctx.tool_receipts)

        # 调用模型（含工具执行循环）
        result = self._call_model(role, policy, model_ctx, user_message, tool_ctx)

        # 提取本节点的工具回执
        node_receipts = tool_ctx.tool_receipts[pre_count:]

        # 后置校验
        post_err = NodeValidator.post_check(
            state, policy,
            result if result else None,
            node_receipts,
        )

        # ADR-0003 §1：必需工具漏调（模型不合规，非健康/基础设施错误）时，
        # 回滚本节点回执，用干净上下文重试，最多 2 次，提升演示可靠性。
        if post_err and post_err.error_code == "REQUIRED_TOOL_NOT_CALLED":
            for _ in range(2):
                del tool_ctx.tool_receipts[pre_count:]
                retry_result = self._call_model(role, policy, model_ctx, user_message, tool_ctx)
                retry_receipts = tool_ctx.tool_receipts[pre_count:]
                retry_err = NodeValidator.post_check(
                    state, policy,
                    retry_result if retry_result else None,
                    retry_receipts,
                )
                if retry_err is None:
                    result = retry_result
                    node_receipts = retry_receipts
                    post_err = None
                    break
                if retry_err.error_code != "REQUIRED_TOOL_NOT_CALLED":
                    post_err = retry_err
                    break  # 其他错误不再重试

        if post_err:
            state.status = RequestStatus.FAILED
            state.error = post_err
            return result

        # 本节点工具回执写入 WorkflowState
        for rd in node_receipts:
            if rd and isinstance(rd, dict):
                state.tool_receipts.append(ToolReceipt(
                    receipt_id=str(uuid.uuid4())[:8],
                    tool_name=rd.get("tool_name", "unknown"),
                    called_by_node=state.current_node or NodeType.QUERY_UNDERSTANDING,
                    role=role,
                    parameter_hash="",
                    result_hash="",
                ))

        return result

    def _call_model(
        self, role: str, policy: RolePolicy, model_ctx,
        user_input: str, tool_ctx: ToolContext,
    ) -> dict:
        """调用 LLM——函数调用模式。"""
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

        # 工具循环：每轮执行模型发起的工具调用后，检查必需工具缺口并提示，
        # 防止模型反复调用非必需工具而漏掉必需工具（INV-007）。
        for _round in range(5):
            try:
                response = self._llm.invoke(
                    role, system_prompt, full_user,
                    tools=tool_defs if tool_defs else None,
                )
            except Exception as e:
                return {"status": "failed", "error": str(e), "content": ""}

            tool_calls = response.get("tool_calls", [])
            content = response.get("content", "")

            if tool_calls:
                # 执行工具
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

                # 将结果追加回上下文（清晰格式）
                results_text = "\n".join(
                    f"[{tr['tool']}] {json.dumps(tr.get('result', tr.get('error', '')), ensure_ascii=False, default=str)[:500]}"
                    for tr in tool_results
                )
                full_user += f"\n\n## 工具执行结果\n{results_text}"

            # 每轮检查必需工具缺口（无论本轮是否调用工具）
            required = set(policy.required_tool_receipts)
            called = {r.get("tool_name") for r in tool_ctx.tool_receipts}
            missing = required - called
            if missing and _round < 4:
                full_user += (
                    f"\n\n## 注意\n你还没有调用以下必需工具：{', '.join(sorted(missing))}。"
                    f"必须先调用它们获得结果，再输出最终 JSON。"
                )
                continue

            # 空内容 nudge：模型调完工具后可能输出空串，提示其输出最终 JSON
            content_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if not content_text.strip() and _round < 4:
                full_user += (
                    "\n\n## 注意\n你的输出为空。请基于上面的工具执行结果，"
                    "输出符合角色要求的最终结构化 JSON，不要留空。"
                )
                continue

            return self._parse_result(content, role)

        return {"status": "ok", "content": ""}

    def _build_tool_defs(self, policy: RolePolicy) -> list[dict] | None:
        if not policy.allowed_tools:
            return None

        # 工具描述——让模型自主判断何时调用哪个工具
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
        # 从 markdown 代码块 / 正文中提取 JSON 对象
        match = _re.search(r'\{.*\}', content, _re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass
        return {"content": content}

    def _finalize(self, state: WorkflowState, request_id: str,
                  c4: ContextService) -> None:
        """终态提交。"""
        if not state.is_terminal():
            original = state.status
            state.status = RequestStatus.FAILED
            state.error = state.error or WorkflowError(
                "WORKFLOW_TERMINAL_VIOLATION",
                f"Non-terminal at commit: {original}"
            )
        status = state.status.value

        # INV-010：completed 时把最终结果 + 健康审计原子提交到 MySQL；
        # 强制审计失败 → 不保留成功结果，转为 failed（AUDIT_COMMIT_FAILED）。
        if status == "completed":
            try:
                from food_agent_v2.application import commit_request_result
                fva = state.final_validation_artifact or {}
                health_evidence = {
                    "plan_id": fva.get("plan_id", ""),
                    "recipe_ids": fva.get("recipe_ids", []),
                    "final_validation_verdict": fva.get("verdict", ""),
                    "review_verdict": (state.review_artifact or {}).get("verdict", ""),
                    "answer_nonempty": bool(
                        (state.answer_artifact or {}).get("content", "")),
                    "tool_receipt_count": len(state.tool_receipts),
                }
                commit_request_result(
                    request_id=request_id,
                    session_id=state.shared_context_ref or "",
                    status="completed",
                    final_plan_id=fva.get("plan_id", ""),
                    health_evidence=health_evidence,
                    participant_refs=state.participant_refs,
                )
            except Exception as e:  # noqa: BLE001
                state.status = RequestStatus.FAILED
                state.error = state.error or WorkflowError(
                    "AUDIT_COMMIT_FAILED", str(e))
                status = state.status.value

        d1_api.update_status(request_id, status,
                            result_summary={"status": status},
                            error={"code": state.error.error_code,
                                   "message": state.error.message}
                            if state.error else None)
        c4.commit_session_state(request_id, status)
