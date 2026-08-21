"""C3 WorkflowState 与纯 Reducer（T16）。

- 所有状态更新经纯 `reduce_workflow_state`（返回新状态，不修改原 state）；
- 模型不能直接写 state（`can_model_write_state` 恒为 False）；
- RequestStatus 使用 contracts/status.py 的封闭 11 值枚举。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class RequestStatus(StrEnum):
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


class NodeType(StrEnum):
    CONTEXT_BUILDING = "context_building"
    QUERY_UNDERSTANDING = "query_understanding"
    HEALTH_MENU_PLANNING = "health_menu_planning"
    MENU_DECISION = "menu_decision"
    ANSWER_GENERATION = "answer_generation"
    UNIFIED_REVIEW = "unified_review"
    ATOMIC_COMMIT = "atomic_commit"


TERMINAL_STATUSES = {
    RequestStatus.COMPLETED,
    RequestStatus.NO_SAFE_MENU,
    RequestStatus.NO_FEASIBLE_MENU,
    RequestStatus.NEEDS_CLARIFICATION,
    RequestStatus.FAILED,
    RequestStatus.CANCELLED,
    RequestStatus.INTERRUPTED,
}


@dataclass
class WorkflowError:
    error_code: str
    message: str
    failed_node: NodeType | None = None
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class WorkflowState:
    """请求工作流状态。仅能通过纯 reducer 更新。"""

    request_id: str
    status: RequestStatus = RequestStatus.ACCEPTED
    current_node: NodeType | None = None
    previous_node: NodeType | None = None
    shared_context_ref: str | None = None
    participant_refs: list[str] = field(default_factory=list)
    build_id: str = ""  # 请求的构建身份（T17 由 runner 注入；空值即未绑定 → 校验 fail-closed）

    query_plan_artifact: dict | None = None
    health_evaluation_artifact: dict | None = None
    feasible_menu_artifact: dict | None = None
    menu_decision_artifact: dict | None = None
    final_validation_artifact: dict | None = None
    answer_artifact: dict | None = None
    review_artifact: dict | None = None

    tool_receipts: list[Any] = field(default_factory=list)

    retrieval_expansion_count: int = 0
    health_replan_count: int = 0
    review_revision_count: int = 0
    review_reevaluation_count: int = 0

    stage_events: list[dict] = field(default_factory=list)
    next_event_id: int = 1
    cancel_requested: bool = False
    error: WorkflowError | None = None
    updated_at: float = field(default_factory=time.time)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def can_model_write_state() -> bool:
    """模型永远不能直接写 WorkflowState（INV-006）。"""
    return False


def _next_counter(state: WorkflowState, counter_name: str, limit: int) -> tuple[int, bool]:
    """纯计算计数器：未超限返回 (新值, True)，超限返回 (当前值, False)。"""
    current = getattr(state, counter_name, 0)
    if current >= limit:
        return current, False
    return current + 1, True


_ARTIFACT_FIELD = {
    "query_plan": "query_plan_artifact",
    "health_evaluation": "health_evaluation_artifact",
    "feasible_menu": "feasible_menu_artifact",
    "menu_decision": "menu_decision_artifact",
    "final_validation": "final_validation_artifact",
    "answer": "answer_artifact",
    "review": "review_artifact",
}


def reduce_workflow_state(state: WorkflowState, *, action: str, **params) -> WorkflowState:
    """纯 reducer：按 action 返回新状态，绝不修改原 state。

    转换 action（context_building/query_understanding/health_menu_planning/
    menu_decision/answer_generation/unified_review/atomic_commit）决定终态与
    下一节点；runner 支持 action（set_node/set_context_ref/set_artifact/
    record_receipts/set_status/fail）用于节点入口与 Artifact/回执登记。
    必需工具失败/漏调路径在校验层（receipts/validator）立即失败，不在此自动重试。
    """
    if action == "context_building":
        if not params.get("manifest_valid"):
            return replace(
                state,
                status=RequestStatus.FAILED,
                current_node=NodeType.ATOMIC_COMMIT,
                error=WorkflowError("CONTEXT_INTEGRITY_FAILED", "上下文完整性校验失败"),
            )
        return replace(state, current_node=NodeType.QUERY_UNDERSTANDING)

    if action == "query_understanding":
        if params.get("needs_clarification"):
            return replace(state, status=RequestStatus.NEEDS_CLARIFICATION, current_node=NodeType.ATOMIC_COMMIT)
        if not params.get("success"):
            return replace(state, status=RequestStatus.FAILED, current_node=NodeType.ATOMIC_COMMIT)
        return replace(state, current_node=NodeType.HEALTH_MENU_PLANNING)

    if action == "health_menu_planning":
        result = params.get("result", "failed")
        if result == "no_safe_menu":
            return replace(state, status=RequestStatus.NO_SAFE_MENU, current_node=NodeType.ATOMIC_COMMIT)
        if result == "no_feasible_menu":
            return replace(state, status=RequestStatus.NO_FEASIBLE_MENU, current_node=NodeType.ATOMIC_COMMIT)
        if result == "needs_expansion":
            new_count, ok = _next_counter(state, "retrieval_expansion_count", 1)
            if ok:
                return replace(
                    state, status=RequestStatus.REVISING,
                    current_node=NodeType.HEALTH_MENU_PLANNING,
                    retrieval_expansion_count=new_count,
                )
            return replace(
                state, status=RequestStatus.FAILED, current_node=NodeType.ATOMIC_COMMIT,
                error=WorkflowError("WORKFLOW_RETRY_LIMIT_EXCEEDED", "扩展召回已达上限"),
            )
        if result == "ok":
            return replace(state, current_node=NodeType.MENU_DECISION)
        return replace(state, status=RequestStatus.FAILED, current_node=NodeType.ATOMIC_COMMIT)

    if action == "menu_decision":
        if params.get("needs_replan"):
            new_count, ok = _next_counter(state, "health_replan_count", 1)
            if ok:
                return replace(
                    state, status=RequestStatus.REVISING,
                    current_node=NodeType.HEALTH_MENU_PLANNING,
                    health_replan_count=new_count,
                )
            return replace(
                state, status=RequestStatus.FAILED, current_node=NodeType.ATOMIC_COMMIT,
                error=WorkflowError("WORKFLOW_RETRY_LIMIT_EXCEEDED", "重新规划已达上限"),
            )
        if not params.get("validation_pass"):
            return replace(
                state, status=RequestStatus.FAILED, current_node=NodeType.ATOMIC_COMMIT,
                error=WorkflowError("FINAL_HEALTH_VALIDATION_FAILED", "最终健康校验未通过"),
            )
        return replace(state, current_node=NodeType.ANSWER_GENERATION)

    if action == "answer_generation":
        if not params.get("success"):
            return replace(state, status=RequestStatus.FAILED, current_node=NodeType.ATOMIC_COMMIT)
        return replace(state, current_node=NodeType.UNIFIED_REVIEW)

    if action == "unified_review":
        status = params.get("status", "FAILED")
        if status == "PASS":
            return replace(state, status=RequestStatus.COMPLETED, current_node=NodeType.ATOMIC_COMMIT)
        if status == "REVISION_REQUIRED":
            target = params.get("target_node")
            # 定向修订（文档 §13.2）：第一次按 target_node 路由。
            # target_node=answer_generation → 改回答；menu_decision → 改菜单；
            # 上游节点/证据缺失 → 不进入修订，直接 failed。
            if state.review_revision_count == 0:
                if target == "answer_generation":
                    return replace(
                        state, status=RequestStatus.REVISING,
                        current_node=NodeType.ANSWER_GENERATION,
                        review_revision_count=1,
                    )
                if target == "menu_decision":
                    return replace(
                        state, status=RequestStatus.REVISING,
                        current_node=NodeType.MENU_DECISION,
                        review_revision_count=1,
                    )
                return replace(
                    state, status=RequestStatus.FAILED, current_node=NodeType.ATOMIC_COMMIT,
                    error=WorkflowError("REVIEW_TARGET_INVALID",
                                        f"审查要求修订到未支持的目标节点: {target}"),
                )
            # 复审：修订后仍 REVISION_REQUIRED，再给一次机会回到 answer_generation。
            if state.review_reevaluation_count == 0:
                return replace(
                    state, status=RequestStatus.REVISING,
                    current_node=NodeType.ANSWER_GENERATION,
                    review_reevaluation_count=1,
                )
            # 复审仍 REVISION_REQUIRED → 修订上限耗尽
            return replace(
                state, status=RequestStatus.FAILED, current_node=NodeType.ATOMIC_COMMIT,
                error=WorkflowError("WORKFLOW_RETRY_LIMIT_EXCEEDED", "复审仍不通过"),
            )
        return replace(state, status=RequestStatus.FAILED, current_node=NodeType.ATOMIC_COMMIT)

    if action == "atomic_commit":
        return replace(state, current_node=NodeType.ATOMIC_COMMIT)

    # ---- runner 支持动作 ----

    if action == "set_node":
        return replace(state, current_node=params["node"])

    if action == "set_context_ref":
        return replace(state, shared_context_ref=params["shared_context_ref"])

    if action == "set_status":
        return replace(
            state,
            status=params["status"],
            current_node=params.get("node") or state.current_node,
        )

    if action == "set_artifact":
        field_name = _ARTIFACT_FIELD.get(params["artifact"])
        if field_name is None:
            raise ValueError(f"未知 artifact: {params['artifact']}")
        return replace(state, **{field_name: params["value"]})

    if action == "record_receipts":
        return replace(
            state,
            tool_receipts=list(state.tool_receipts) + list(params["receipts"]),
        )

    if action == "fail":
        return replace(
            state,
            status=RequestStatus.FAILED,
            current_node=params.get("node") or state.current_node,
            error=params["error"],
        )

    raise ValueError(f"未知 reducer action: {action}")
