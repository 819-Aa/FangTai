"""Agent 执行策略、门卫校验、前置条件与预算控制（Section 6.2 & Section 12）。"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from food_agent_v2.c3.agent_actions import (
    ActionType,
    AgentAction,
    AskUserArgs,
    AuditRecipeHealthArgs,
    CombineNutritionalMenuArgs,
    ExpandCandidatesArgs,
    FinishArgs,
    ReadMenuArgs,
    SearchCandidatesArgs,
    ValidateSelectedMenuArgs,
)
from food_agent_v2.contracts.build import canonical_json_hash


class GateCheckResult:
    def __init__(
        self,
        allowed: bool,
        error_code: str | None = None,
        error_message: str = "",
        validated_args: Any = None,
    ) -> None:
        self.allowed = allowed
        self.error_code = error_code
        self.error_message = error_message
        self.validated_args = validated_args


class AgentPolicy:
    """Agent 执行策略与门卫。"""

    MAX_DECISIONS = 12
    MAX_TOOL_EXECUTIONS = 10
    MAX_EXPANSIONS = 1
    MAX_HEALTH_REVISIONS = 1
    MAX_REPLANS = MAX_HEALTH_REVISIONS

    def __init__(
        self,
        max_decisions: int = MAX_DECISIONS,
        max_tool_executions: int = MAX_TOOL_EXECUTIONS,
        max_expansions: int = MAX_EXPANSIONS,
        max_health_revisions: int = MAX_HEALTH_REVISIONS,
        max_replans: int | None = None,
    ) -> None:
        self.max_decisions = max_decisions
        self.max_tool_executions = max_tool_executions
        self.max_expansions = max_expansions
        self.max_health_revisions = (
            max_replans if max_replans is not None else max_health_revisions
        )
        self.max_replans = self.max_health_revisions

        # 运行时计数器
        self.decision_count = 0
        self.tool_execution_count = 0
        self.expansion_count = 0
        self.combine_count = 0
        self.health_revision_count = 0

        # 重复调用记录：(action, canonical_args_hash)
        self._executed_actions: set[tuple[str, str]] = set()

    @property
    def replan_count(self) -> int:
        return self.health_revision_count

    @replan_count.setter
    def replan_count(self, value: int) -> None:
        self.health_revision_count = value

    def check_decision_budget(self) -> GateCheckResult:
        """检查 Agent 决策轮次预算。"""
        self.decision_count += 1
        if self.decision_count > self.max_decisions:
            return GateCheckResult(
                allowed=False,
                error_code="DECISION_BUDGET_EXCEEDED",
                error_message=f"Agent 决策轮次超出上限 ({self.max_decisions})",
            )
        return GateCheckResult(allowed=True)

    def validate_action_gate(
        self,
        action: AgentAction,
        execution_context: dict[str, Any],
    ) -> GateCheckResult:
        """门卫全面检查：白名单、参数 Schema、前置条件、证据真实性、预算及重复调用控制。"""
        # 1. 白名单与参数 Schema 校验（禁止额外字段）
        args = action.arguments or {}
        try:
            if action.action == ActionType.READ_MENU:
                validated_args = ReadMenuArgs(**args)
            elif action.action == ActionType.SEARCH_CANDIDATES:
                validated_args = SearchCandidatesArgs(**args)
            elif action.action == ActionType.EXPAND_CANDIDATES:
                validated_args = ExpandCandidatesArgs(**args)
            elif action.action == ActionType.AUDIT_RECIPE_HEALTH:
                validated_args = AuditRecipeHealthArgs(**args)
            elif action.action == ActionType.COMBINE_NUTRITIONAL_MENU:
                validated_args = CombineNutritionalMenuArgs(**args)
            elif action.action == ActionType.VALIDATE_SELECTED_MENU:
                validated_args = ValidateSelectedMenuArgs(**args)
            elif action.action == ActionType.ASK_USER:
                validated_args = AskUserArgs(**args)
            elif action.action == ActionType.FINISH:
                validated_args = FinishArgs(**args)
            else:
                return GateCheckResult(
                    allowed=False,
                    error_code="UNAUTHORIZED_ACTION",
                    error_message=f"未授权的动作类型: {action.action}",
                )
        except ValidationError as exc:
            return GateCheckResult(
                allowed=False,
                error_code="INVALID_ACTION_ARGUMENTS",
                error_message=f"动作参数 Schema 校验失败: {exc}",
            )

        # 2. 检查工具执行预算
        is_tool = action.action not in (ActionType.ASK_USER, ActionType.FINISH)
        if is_tool:
            if self.tool_execution_count >= self.max_tool_executions:
                return GateCheckResult(
                    allowed=False,
                    error_code="TOOL_BUDGET_EXCEEDED",
                    error_message=f"工具执行次数超出上限 ({self.max_tool_executions})",
                )

        # 3. 检查特定工具频次控制（如扩展召回、重新规划）
        if action.action == ActionType.EXPAND_CANDIDATES:
            if self.expansion_count >= self.max_expansions:
                return GateCheckResult(
                    allowed=False,
                    error_code="EXPANSION_BUDGET_EXCEEDED",
                    error_message=f"补充搜索召回超出上限 ({self.max_expansions})",
                )
        elif action.action == ActionType.COMBINE_NUTRITIONAL_MENU:
            # 区分“候选变化后再次规划”与“最终健康排除后的修订”
            # 由执行器在 execution_context 传入根据已验证失败回执判定的 is_health_revision
            is_health_revision = execution_context.get("is_health_revision", False)
            if is_health_revision and self.health_revision_count >= self.max_health_revisions:
                return GateCheckResult(
                    allowed=False,
                    error_code="HEALTH_REVISION_BUDGET_EXCEEDED",
                    error_message=f"最终健康排除后的重新规划次数超出上限 ({self.max_health_revisions})",
                )

        # 4. 重复调用控制（相同动作、规范化参数与证据引用）
        if is_tool:
            ctx = execution_context or {}
            norm_hash = canonical_json_hash({
                "args": args,
                "evidence_refs": sorted(action.evidence_refs or []),
                "is_health_revision": bool(ctx.get("is_health_revision")),
            })
            action_key = (action.action.value, norm_hash)
            if action_key in self._executed_actions:
                return GateCheckResult(
                    allowed=False,
                    error_code="DUPLICATE_TOOL_CALL",
                    error_message=f"禁止重复执行相同参数与证据的工具: {action.action.value}",
                )

        # 5. 前置条件与证据存在性检查
        known_evidence = execution_context.get("known_evidence", set())
        for ref in action.evidence_refs:
            if ref and ref not in known_evidence:
                return GateCheckResult(
                    allowed=False,
                    error_code="INVALID_EVIDENCE_REFERENCE",
                    error_message=f"引用的证据不存在或未由系统验证: {ref}",
                )

        # 前置依赖条件
        if action.action == ActionType.AUDIT_RECIPE_HEALTH:
            # 必须已有候选（由检索或读取菜单提供）
            known_candidates = execution_context.get("known_candidates", set())
            req_ids = set(validated_args.candidate_recipe_ids or [])
            if not req_ids:
                req_ids = known_candidates
            if not req_ids:
                return GateCheckResult(
                    allowed=False,
                    error_code="NO_CANDIDATES_TO_AUDIT",
                    error_message="尚未获取候选菜谱，无法执行健康审核",
                )
            # 不能凭空创造不存在于候选池的菜品 ID
            if req_ids - known_candidates:
                return GateCheckResult(
                    allowed=False,
                    error_code="UNAUTHORIZED_RECIPE_IDS",
                    error_message="审核包含了未检索的非法菜品 ID",
                )

        elif action.action == ActionType.COMBINE_NUTRITIONAL_MENU:
            # 必须已有健康审核结果
            has_audit = execution_context.get("health_evaluated", False)
            if not has_audit:
                return GateCheckResult(
                    allowed=False,
                    error_code="AUDIT_REQUIRED_BEFORE_COMBINE",
                    error_message="必须先完成健康审核才能进行菜单规划",
                )

        elif action.action == ActionType.VALIDATE_SELECTED_MENU:
            # 必须针对已生成的真实可行方案
            valid_plans = execution_context.get("feasible_plan_ids", set())
            if validated_args.plan_id not in valid_plans:
                return GateCheckResult(
                    allowed=False,
                    error_code="UNKNOWN_PLAN_ID",
                    error_message=f"校验的 plan_id 不在已有可行方案中: {validated_args.plan_id}",
                )

        elif action.action == ActionType.ASK_USER:
            opts = validated_args.options
            if len(opts) < 2 or len(opts) > 3:
                return GateCheckResult(
                    allowed=False,
                    error_code="INVALID_OPTION_COUNT",
                    error_message=f"澄清选项数量必须为 2~3 项，当前为 {len(opts)} 项",
                )
            allowed_mods = {
                "dish_count", "dish_count_requested",
                "time_constraint_seconds", "time_constraint_policy",
                "dish_types", "meal_type", "meal_types", "taste_tags",
                "expand_search", "re_search",
            }
            for opt in opts:
                mods = opt.get("modifications", {})
                if not isinstance(mods, dict):
                    return GateCheckResult(
                        allowed=False,
                        error_code="INVALID_OPTION_MODIFICATIONS",
                        error_message="选项的 modifications 必须为字典",
                    )
                unsupported = set(mods.keys()) - allowed_mods
                if unsupported:
                    return GateCheckResult(
                        allowed=False,
                        error_code="UNSUPPORTED_MODIFICATION_FIELD",
                        error_message=f"选项包含不支持的修改字段: {unsupported}",
                    )

        elif action.action == ActionType.FINISH:
            # 显式校验回执必须存在且为 PASS
            final_val = execution_context.get("final_validation")
            if not final_val:
                return GateCheckResult(
                    allowed=False,
                    error_code="FINAL_HEALTH_VALIDATION_MISSING",
                    error_message="尚未显式调用 validate_selected_menu，严禁提前完成 (INV-007)",
                )
            if getattr(final_val, "status", "") != "PASS":
                return GateCheckResult(
                    allowed=False,
                    error_code="FINAL_HEALTH_VALIDATION_NOT_PASSED",
                    error_message=f"最终健康校验未通过 (status: {getattr(final_val, 'status', '')})",
                )
            if getattr(final_val, "plan_id", "") != validated_args.plan_id:
                return GateCheckResult(
                    allowed=False,
                    error_code="PLAN_ID_MISMATCH",
                    error_message="finish 的 plan_id 与最终校验的 plan_id 不一致",
                )
            if validated_args.final_validation_ref != str(getattr(final_val, "artifact_id", "")):
                return GateCheckResult(
                    allowed=False,
                    error_code="INVALID_EVIDENCE_REFERENCE",
                    error_message="finish 必须引用当前最终校验产物",
                )

        return GateCheckResult(allowed=True, validated_args=validated_args)

    def record_tool_execution(
        self, action: AgentAction, execution_context: dict[str, Any] | None = None
    ) -> None:
        """记录工具执行并更新预算。"""
        is_tool = action.action not in (ActionType.ASK_USER, ActionType.FINISH)
        if is_tool:
            self.tool_execution_count += 1
            ctx = execution_context or {}
            norm_hash = canonical_json_hash({
                "args": action.arguments or {},
                "evidence_refs": sorted(action.evidence_refs or []),
                "is_health_revision": bool(ctx.get("is_health_revision")),
            })
            self._executed_actions.add((action.action.value, norm_hash))
            if action.action == ActionType.EXPAND_CANDIDATES:
                self.expansion_count += 1
            elif action.action == ActionType.COMBINE_NUTRITIONAL_MENU:
                self.combine_count += 1
                ctx = execution_context or {}
                if ctx.get("is_health_revision"):
                    self.health_revision_count += 1
