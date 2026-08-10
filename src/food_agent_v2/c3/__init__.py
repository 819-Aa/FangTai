"""C3 Agent 工作流引擎（T16）。

五模型编排、状态机、角色策略、工具权限。状态更新经纯 reducer
（state.reduce_workflow_state）；工具回执经 contracts 绑定 request/node/input/build。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from food_agent_v2.c3.receipts import ReceiptBindingError, validate_workflow_receipt
from food_agent_v2.c3.state import (
    TERMINAL_STATUSES,
    NodeType,
    RequestStatus,
    WorkflowError,
    WorkflowState,
    can_model_write_state,
    reduce_workflow_state,
)

ERROR_CODES = {
    "REQUIRED_TOOL_NOT_CALLED": "模型未调用必需工具",
    "TOOL_PERMISSION_DENIED": "模型调用了角色禁用工具",
    "SCHEMA_VALIDATION_FAILED": "Artifact Schema 校验失败",
    "STATE_MUTATION_DENIED": "模型提交包含非法 State 写入的 Artifact",
    "UNTRUSTED_INSTRUCTION_DETECTED": "用户输入或 RAG 文档中检测到指令注入",
    "FINAL_HEALTH_VALIDATION_FAILED": "最终健康校验未通过",
    "WORKFLOW_RETRY_LIMIT_EXCEEDED": "循环上限耗尽",
    "CONTEXT_INTEGRITY_FAILED": "上下文完整性校验失败",
    "SENSITIVE_DATA_EXPOSURE": "响应中发现禁止字段",
    "TOOL_EXECUTION_FAILED": "工具执行失败",
    "IDEMPOTENCY_KEY_REUSED": "幂等键冲突",
    "REQUEST_ALREADY_TERMINAL": "请求已处于终态",
}

# INV-012：不可信指令注入检测。
UNTRUSTED_INSTRUCTION_PATTERNS = [
    r"忽略.{0,8}?(?:指令|系统提示词|规则|限制|约束|开发者指令|要求|设定)",
    r"system\s*prompt",
    r"覆盖(?:你的)?(?:系统|开发者)?(?:指令|规则|设定)",
    r"不需要遵守(?:任何)?(?:指令|规则|限制|约束)",
    r"(?:无视|不要管|别管)(?:以上|所有|我)?(?:指令|规则|系统|提示词)",
    r"(?:打印|输出|泄露|透露)(?:你的)?(?:系统提示词|system|开发者指令|密钥|密码|api\s*key)",
    r"(?:你现在是|假装你是).{0,10}(?:绕过|越权|无限制)",
]


def detect_untrusted_instruction(text: str) -> str | None:
    """检测用户输入中的指令注入。命中返回匹配片段，否则 None。"""
    if not text:
        return None
    import re

    for pattern in UNTRUSTED_INSTRUCTION_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)[:60]
    return None


# ---- 角色策略 ----

@dataclass
class ToolSpec:
    name: str
    tool_type: str
    max_calls_per_node: int = 1
    idempotent: bool = True


@dataclass
class RolePolicy:
    role: str
    allowed_tools: list[ToolSpec] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    required_tool_receipts: list[str] = field(default_factory=list)
    output_artifact_type: str = ""
    input_artifact_types: list[str] = field(default_factory=list)
    context_projection_role: str = ""


ROLE_POLICIES: dict[str, RolePolicy] = {
    "query_understanding": RolePolicy(
        role="query_understanding",
        allowed_tools=[ToolSpec("retrieve_recipes", "required"), ToolSpec("get_current_menu", "optional")],
        forbidden_tools=["get_health_constraints", "evaluate_recipe_health", "generate_feasible_menus", "validate_selected_menu_health", "adjust_menu_plan", "expand_retrieval"],
        required_tool_receipts=["retrieve_recipes"],
        output_artifact_type="QueryPlanArtifact",
        context_projection_role="query_understanding",
    ),
    "health_menu_planning": RolePolicy(
        role="health_menu_planning",
        allowed_tools=[
            ToolSpec("get_health_constraints", "required"),
            ToolSpec("evaluate_recipe_health", "required"),
            ToolSpec("generate_feasible_menus", "required"),
            ToolSpec("expand_retrieval", "optional"),
            ToolSpec("adjust_menu_plan", "optional"),
        ],
        forbidden_tools=["validate_selected_menu_health"],
        required_tool_receipts=["get_health_constraints", "evaluate_recipe_health", "generate_feasible_menus"],
        output_artifact_type="HealthEvaluationArtifact|FeasibleMenuArtifact",
        input_artifact_types=["QueryPlanArtifact"],
        context_projection_role="health_menu_planning",
    ),
    "menu_decision": RolePolicy(
        role="menu_decision",
        allowed_tools=[ToolSpec("validate_selected_menu_health", "required")],
        forbidden_tools=["get_health_constraints", "evaluate_recipe_health", "generate_feasible_menus"],
        required_tool_receipts=["validate_selected_menu_health"],
        output_artifact_type="MenuDecisionArtifact",
        input_artifact_types=["HealthEvaluationArtifact", "FeasibleMenuArtifact"],
        context_projection_role="menu_decision",
    ),
    "answer_generation": RolePolicy(
        role="answer_generation",
        allowed_tools=[],
        forbidden_tools=["get_health_constraints", "evaluate_recipe_health", "generate_feasible_menus", "validate_selected_menu_health", "retrieve_recipes"],
        required_tool_receipts=[],
        output_artifact_type="AnswerArtifact",
        input_artifact_types=["MenuDecisionArtifact"],
        context_projection_role="answer_generation",
    ),
    "unified_review": RolePolicy(
        role="unified_review",
        allowed_tools=[ToolSpec("get_execution_trace", "required"), ToolSpec("get_artifact_chain", "optional")],
        forbidden_tools=["get_health_constraints", "evaluate_recipe_health", "generate_feasible_menus", "validate_selected_menu_health", "retrieve_recipes"],
        required_tool_receipts=["get_execution_trace"],
        output_artifact_type="ReviewArtifact",
        input_artifact_types=["AnswerArtifact"],
        context_projection_role="unified_review",
    ),
}


# ---- 节点前置/后置校验（必需工具失败/漏调立即失败，不自动重试）----

class NodeValidator:
    @staticmethod
    def pre_check(state: WorkflowState, policy: RolePolicy) -> WorkflowError | None:
        if state.cancel_requested:
            return WorkflowError("REQUEST_CANCELLED", "请求已取消", failed_node=state.current_node)
        if state.is_terminal():
            return WorkflowError("REQUEST_ALREADY_TERMINAL", "请求已终态")
        for art_type in policy.input_artifact_types:
            if not state._get_artifact_by_type(art_type):
                return WorkflowError("SCHEMA_VALIDATION_FAILED", f"缺少前置 Artifact: {art_type}", failed_node=state.current_node)
        return None

    @staticmethod
    def post_check(state: WorkflowState, policy: RolePolicy, output_artifact: dict | None, tool_receipts: list) -> WorkflowError | None:
        received: dict[str, bool] = {}
        for r in tool_receipts:
            # INV-007：必需工具回执必须携带与 State 一致的完整身份（request/node/input/build）
            identity_err = NodeValidator._check_receipt_identity(state, r)
            if identity_err:
                return identity_err
            name = _receipt_get(r, "tool_name") or ""
            if not name:
                continue
            success = bool(r.success) if hasattr(r, "success") else bool(r.get("success", True))
            received[name] = received.get(name, True) and success
        for required in policy.required_tool_receipts:
            if required not in received:
                return WorkflowError("REQUIRED_TOOL_NOT_CALLED", f"未调用必需工具: {required}", failed_node=state.current_node)
            if not received[required]:
                return WorkflowError("TOOL_EXECUTION_FAILED", f"必需工具执行失败: {required}", failed_node=state.current_node)
        if policy.output_artifact_type and not output_artifact:
            return WorkflowError("SCHEMA_VALIDATION_FAILED", "缺少输出 Artifact", failed_node=state.current_node)
        return None

    @staticmethod
    def _check_receipt_identity(state: WorkflowState, r: Any) -> WorkflowError | None:
        """回执必须携带完整且与当前 State 一致的 request/node/build/input 身份。

        fail-closed：任一字段缺失、非法或与 State 不一致即拒绝（RECEIPT_BINDING_MISMATCH），
        跨 request/node/build 的伪造回执无法通过必需工具检查。
        """
        request_id = _receipt_get(r, "request_id")
        node_id = _receipt_get(r, "node_id")
        build_id = _receipt_get(r, "build_id")
        input_hash = _receipt_get(r, "input_hash")
        for field_name, value in (("request_id", request_id), ("node_id", node_id),
                                  ("build_id", build_id), ("input_hash", input_hash)):
            if value is None or value == "":
                return WorkflowError(
                    "RECEIPT_BINDING_MISMATCH", f"回执缺少绑定身份: {field_name}",
                    failed_node=state.current_node,
                )
        if str(request_id) != str(state.request_id):
            return WorkflowError("RECEIPT_BINDING_MISMATCH", "回执 request_id 与 State 不一致", failed_node=state.current_node)
        if str(node_id) != str(state.current_node or ""):
            return WorkflowError("RECEIPT_BINDING_MISMATCH", "回执 node_id 与 State 不一致", failed_node=state.current_node)
        if not state.build_id:
            return WorkflowError("RECEIPT_BINDING_MISMATCH", "State 未注入 build_id，拒绝未绑定回执", failed_node=state.current_node)
        if str(build_id) != str(state.build_id):
            return WorkflowError("RECEIPT_BINDING_MISMATCH", "回执 build_id 与 State 不一致", failed_node=state.current_node)
        if not _is_sha256(input_hash):
            return WorkflowError("RECEIPT_BINDING_MISMATCH", "回执 input_hash 非法（需 64 位十六进制）", failed_node=state.current_node)
        return None


def _receipt_get(receipt: Any, key: str) -> Any:
    """从 dict 或对象回执中读取字段。"""
    if isinstance(receipt, dict):
        return receipt.get(key)
    return getattr(receipt, key, None)


def _is_sha256(value: Any) -> bool:
    """契约 Sha256Hash：64 位十六进制。"""
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdefABCDEF" for c in value))


# WorkflowState 辅助方法
def _get_artifact_by_type(self: WorkflowState, art_type: str) -> dict | None:
    mapping = {
        "QueryPlanArtifact": self.query_plan_artifact,
        "HealthEvaluationArtifact": self.health_evaluation_artifact,
        "FeasibleMenuArtifact": self.feasible_menu_artifact,
        "MenuDecisionArtifact": self.menu_decision_artifact,
        "FinalValidationArtifact": self.final_validation_artifact,
        "AnswerArtifact": self.answer_artifact,
        "ReviewArtifact": self.review_artifact,
    }
    return mapping.get(art_type)


WorkflowState._get_artifact_by_type = _get_artifact_by_type  # type: ignore[attr-defined]


__all__ = [
    "ERROR_CODES",
    "NodeType",
    "NodeValidator",
    "ROLE_POLICIES",
    "ReceiptBindingError",
    "RequestStatus",
    "RolePolicy",
    "TERMINAL_STATUSES",
    "ToolSpec",
    "UNTRUSTED_INSTRUCTION_PATTERNS",
    "WorkflowError",
    "WorkflowState",
    "can_model_write_state",
    "detect_untrusted_instruction",
    "reduce_workflow_state",
    "validate_workflow_receipt",
]
