"""C3 Agent 工作流引擎 —— 五模型编排、状态机、角色策略、工具权限。

定义工作流的确定性规则：
- 七个节点的前置/后置校验
- 五模型角色策略（允许工具、必需工具、禁止工具）
- 工具注册表（参数 Schema、回执格式、调用预算）
- 状态机转换函数和循环上限
- 终态判定
"""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---- 状态与终态 ----

class RequestStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    REVISING = "revising"
    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    NO_SAFE_MENU = "no_safe_menu"
    NO_FEASIBLE_MENU = "no_feasible_menu"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class NodeType(str, Enum):
    CONTEXT_BUILDING = "context_building"
    QUERY_UNDERSTANDING = "query_understanding"
    HEALTH_MENU_PLANNING = "health_menu_planning"
    MENU_DECISION = "menu_decision"
    ANSWER_GENERATION = "answer_generation"
    UNIFIED_REVIEW = "unified_review"
    ATOMIC_COMMIT = "atomic_commit"


TERMINAL_STATUSES = {
    RequestStatus.COMPLETED, RequestStatus.NO_SAFE_MENU,
    RequestStatus.NO_FEASIBLE_MENU, RequestStatus.NEEDS_CLARIFICATION,
    RequestStatus.FAILED, RequestStatus.CANCELLED, RequestStatus.INTERRUPTED,
}


# ---- 错误码 ----

@dataclass
class WorkflowError:
    error_code: str
    message: str
    failed_node: NodeType | None = None
    evidence_refs: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


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
# 用"指令式组合"降低误报（"忽略甜品"这类正常表达不会命中，只有"忽略指令/系统提示词"等才命中）。
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


# ---- 工具回执 ----

@dataclass
class ToolReceipt:
    receipt_id: str
    tool_name: str
    called_by_node: NodeType
    role: str
    parameter_hash: str
    result_hash: str
    evidence_refs: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @staticmethod
    def hash_params(params: dict) -> str:
        return hashlib.sha256(
            json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]

    @staticmethod
    def hash_result(result: Any) -> str:
        return hashlib.sha256(
            json.dumps(str(result), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]


# ---- WorkflowState ----

@dataclass
class WorkflowState:
    request_id: str
    status: RequestStatus = RequestStatus.ACCEPTED
    current_node: NodeType | None = None
    previous_node: NodeType | None = None
    shared_context_ref: str | None = None
    participant_refs: list[str] = field(default_factory=list)  # 用于 INV-010 审计

    # Artifacts
    query_plan_artifact: dict | None = None
    health_evaluation_artifact: dict | None = None
    feasible_menu_artifact: dict | None = None
    menu_decision_artifact: dict | None = None
    final_validation_artifact: dict | None = None
    answer_artifact: dict | None = None
    review_artifact: dict | None = None

    # 工具回执
    tool_receipts: list[ToolReceipt] = field(default_factory=list)

    # 循环计数器
    retrieval_expansion_count: int = 0
    health_replan_count: int = 0
    review_revision_count: int = 0
    review_reevaluation_count: int = 0

    # SSE 事件游标
    stage_events: list[dict] = field(default_factory=list)
    next_event_id: int = 1

    # 取消
    cancel_requested: bool = False

    # 错误
    error: WorkflowError | None = None
    updated_at: float = field(default_factory=time.time)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def check_and_increment(self, counter_name: str, limit: int) -> bool:
        """检查计数器是否超限。未超限则递增并返回 True。"""
        current = getattr(self, counter_name, 0)
        if current >= limit:
            return False
        setattr(self, counter_name, current + 1)
        return True


# ---- 角色策略 ----

@dataclass
class ToolSpec:
    name: str
    tool_type: str                     # required | optional
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


# 五模型角色策略（对应 C3 文档 §8.2-8.6）
ROLE_POLICIES: dict[str, RolePolicy] = {
    "query_understanding": RolePolicy(
        role="query_understanding",
        allowed_tools=[
            ToolSpec("retrieve_recipes", "required"),
            ToolSpec("get_current_menu", "optional"),
        ],
        forbidden_tools=[
            "get_health_constraints", "evaluate_recipe_health",
            "generate_feasible_menus", "validate_selected_menu_health",
            "adjust_menu_plan", "expand_retrieval",
        ],
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
        forbidden_tools=[
            "validate_selected_menu_health",
        ],
        required_tool_receipts=[
            "get_health_constraints", "evaluate_recipe_health",
            "generate_feasible_menus",
        ],
        output_artifact_type="HealthEvaluationArtifact|FeasibleMenuArtifact",
        input_artifact_types=["QueryPlanArtifact"],
        context_projection_role="health_menu_planning",
    ),
    "menu_decision": RolePolicy(
        role="menu_decision",
        allowed_tools=[
            ToolSpec("validate_selected_menu_health", "required"),
        ],
        forbidden_tools=[
            "get_health_constraints", "evaluate_recipe_health",
            "generate_feasible_menus",
        ],
        required_tool_receipts=["validate_selected_menu_health"],
        output_artifact_type="MenuDecisionArtifact",
        input_artifact_types=["HealthEvaluationArtifact", "FeasibleMenuArtifact"],
        context_projection_role="menu_decision",
    ),
    "answer_generation": RolePolicy(
        role="answer_generation",
        allowed_tools=[],
        forbidden_tools=[
            "get_health_constraints", "evaluate_recipe_health",
            "generate_feasible_menus", "validate_selected_menu_health",
            "retrieve_recipes",
        ],
        required_tool_receipts=[],
        output_artifact_type="AnswerArtifact",
        input_artifact_types=["MenuDecisionArtifact"],
        context_projection_role="answer_generation",
    ),
    "unified_review": RolePolicy(
        role="unified_review",
        allowed_tools=[
            ToolSpec("get_execution_trace", "required"),
            ToolSpec("get_artifact_chain", "optional"),
        ],
        forbidden_tools=[
            "get_health_constraints", "evaluate_recipe_health",
            "generate_feasible_menus", "validate_selected_menu_health",
            "retrieve_recipes",
        ],
        required_tool_receipts=["get_execution_trace"],
        output_artifact_type="ReviewArtifact",
        input_artifact_types=["AnswerArtifact"],
        context_projection_role="unified_review",
    ),
}


# ---- 节点前置/后置校验 ----

class NodeValidator:
    """节点校验器。"""

    @staticmethod
    def pre_check(state: WorkflowState, policy: RolePolicy) -> WorkflowError | None:
        """节点前置校验：上下文完整性、权限。"""
        if state.cancel_requested:
            return WorkflowError("REQUEST_CANCELLED", "请求已取消",
                                failed_node=state.current_node)

        if state.is_terminal():
            return WorkflowError("REQUEST_ALREADY_TERMINAL", "请求已终态")

        # 前置 Artifact 存在性检查
        for art_type in policy.input_artifact_types:
            if not state._get_artifact_by_type(art_type):
                return WorkflowError("SCHEMA_VALIDATION_FAILED",
                                    f"缺少前置 Artifact: {art_type}",
                                    failed_node=state.current_node)

        return None

    @staticmethod
    def post_check(state: WorkflowState, policy: RolePolicy,
                   output_artifact: dict | None,
                   tool_receipts: list[ToolReceipt]) -> WorkflowError | None:
        """节点后置校验：Artifact Schema、必需工具、证据引用。"""
        # 必需工具检查（INV-007：必须存在有效回执；失败回执不满足要求）
        # 兼容 ToolReceipt 对象和 dict
        received: dict[str, bool] = {}
        for r in tool_receipts:
            name = r.tool_name if hasattr(r, 'tool_name') else r.get("tool_name", "")
            if not name:
                continue
            if hasattr(r, 'success'):
                success = bool(r.success)
            else:
                success = bool(r.get("success", True))
            received[name] = received.get(name, True) and success
        for required in policy.required_tool_receipts:
            if required not in received:
                return WorkflowError("REQUIRED_TOOL_NOT_CALLED",
                                    f"未调用必需工具: {required}",
                                    failed_node=state.current_node)
            if not received[required]:
                # 必需工具已调用但执行失败 → 文档 09 §17.2：TOOL_EXECUTION_FAILED → failed
                return WorkflowError("TOOL_EXECUTION_FAILED",
                                    f"必需工具执行失败: {required}",
                                    failed_node=state.current_node)

        # Artifact 存在性
        if policy.output_artifact_type and not output_artifact:
            return WorkflowError("SCHEMA_VALIDATION_FAILED",
                                "缺少输出 Artifact",
                                failed_node=state.current_node)

        return None


# ---- 状态转换 ----

class WorkflowTransition:
    """状态机转换函数集合。"""

    @staticmethod
    def context_building(state: WorkflowState, manifest_valid: bool) -> NodeType:
        if not manifest_valid:
            state.status = RequestStatus.FAILED
            state.error = WorkflowError("CONTEXT_INTEGRITY_FAILED", "上下文完整性校验失败")
            return NodeType.ATOMIC_COMMIT  # 直接结束
        return NodeType.QUERY_UNDERSTANDING

    @staticmethod
    def query_understanding(state: WorkflowState, success: bool,
                           needs_clarification: bool = False) -> NodeType:
        if needs_clarification:
            state.status = RequestStatus.NEEDS_CLARIFICATION
            return NodeType.ATOMIC_COMMIT
        if not success:
            state.status = RequestStatus.FAILED
            return NodeType.ATOMIC_COMMIT
        return NodeType.HEALTH_MENU_PLANNING

    @staticmethod
    def health_menu_planning(state: WorkflowState,
                            result: str) -> NodeType:
        """result: 'ok' | 'no_safe_menu' | 'no_feasible_menu' | 'needs_expansion' | 'failed'"""
        if result == "ok":
            return NodeType.MENU_DECISION
        if result == "no_safe_menu":
            state.status = RequestStatus.NO_SAFE_MENU
            return NodeType.ATOMIC_COMMIT
        if result == "no_feasible_menu":
            state.status = RequestStatus.NO_FEASIBLE_MENU
            return NodeType.ATOMIC_COMMIT
        if result == "needs_expansion":
            if state.check_and_increment("retrieval_expansion_count", 1):
                state.status = RequestStatus.REVISING
                return NodeType.HEALTH_MENU_PLANNING
            else:
                state.error = WorkflowError("WORKFLOW_RETRY_LIMIT_EXCEEDED",
                                           "扩展召回已达上限")
                state.status = RequestStatus.FAILED
                return NodeType.ATOMIC_COMMIT
        state.status = RequestStatus.FAILED
        return NodeType.ATOMIC_COMMIT

    @staticmethod
    def menu_decision(state: WorkflowState, validation_pass: bool,
                     needs_replan: bool = False) -> NodeType:
        if needs_replan:
            if state.check_and_increment("health_replan_count", 1):
                state.status = RequestStatus.REVISING
                return NodeType.HEALTH_MENU_PLANNING
            else:
                state.error = WorkflowError("WORKFLOW_RETRY_LIMIT_EXCEEDED",
                                           "重新规划已达上限")
                state.status = RequestStatus.FAILED
                return NodeType.ATOMIC_COMMIT
        if not validation_pass:
            # 文档 09 §8.4：最终健康校验失败 → FINAL_HEALTH_VALIDATION_FAILED → failed
            # （不是 no_safe_menu —— no_safe_menu 只在 safe_recipe_ids=[] 且 B4 证据完整时成立）
            state.error = WorkflowError("FINAL_HEALTH_VALIDATION_FAILED",
                                       "最终健康校验未通过")
            state.status = RequestStatus.FAILED
            return NodeType.ATOMIC_COMMIT
        return NodeType.ANSWER_GENERATION

    @staticmethod
    def answer_generation(state: WorkflowState, success: bool) -> NodeType:
        if not success:
            state.status = RequestStatus.FAILED
            return NodeType.ATOMIC_COMMIT
        return NodeType.UNIFIED_REVIEW

    @staticmethod
    def unified_review(state: WorkflowState, verdict: str) -> NodeType:
        if verdict == "PASS":
            state.status = RequestStatus.COMPLETED
            return NodeType.ATOMIC_COMMIT
        if verdict == "REVISION_REQUIRED":
            if state.check_and_increment("review_revision_count", 1):
                state.status = RequestStatus.REVISING
                return NodeType.ANSWER_GENERATION
            else:
                state.error = WorkflowError("WORKFLOW_RETRY_LIMIT_EXCEEDED", "修订已达上限")
                state.status = RequestStatus.FAILED
                return NodeType.ATOMIC_COMMIT
        if verdict == "REVIEW_REQUIRED":
            if state.check_and_increment("review_reevaluation_count", 1):
                state.status = RequestStatus.REVISING
                return NodeType.UNIFIED_REVIEW
            else:
                state.error = WorkflowError("WORKFLOW_RETRY_LIMIT_EXCEEDED", "复审已达上限")
                state.status = RequestStatus.FAILED
                return NodeType.ATOMIC_COMMIT
        state.status = RequestStatus.FAILED
        return NodeType.ATOMIC_COMMIT

    @staticmethod
    def atomic_commit(state: WorkflowState) -> None:
        """原子提交——最终状态写入。非终态状态由 runner._finalize 兜底处理。"""
        state.updated_at = time.time()


# WorkflowState 辅助方法
def _get_artifact_by_type(self: WorkflowState, art_type: str) -> dict | None:
    """按类型名获取 Artifact。"""
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


WorkflowState._get_artifact_by_type = _get_artifact_by_type  # type: ignore
