"""澄清链路生命周期契约。

定义 MySQL 澄清账本、会话状态转移、内部快照、公开投影与客户端结构化选择输入。
遵循设计文档：docs/superpowers/specs/2026-09-27-agent-clarification-lifecycle-design.md
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ClarificationQuestionStatus = Literal["pending", "consumed", "superseded", "expired"]


class ClarificationOption(BaseModel):
    """追问选项定义（内部全量定义，含 modifications 字典）。"""

    model_config = ConfigDict(extra="forbid")

    option_id: int
    text: str
    modifications: dict[str, Any] = Field(default_factory=dict)


class ClarificationOptionView(BaseModel):
    """脱敏公开选项视图（仅含编号与显示文字，绝不向客户端暴露内部 modifications 字典）。"""

    model_config = ConfigDict(extra="forbid")

    option_id: int
    text: str


class ClarificationPublicPayload(BaseModel):
    """问题公开负载：展示给用户的问句与选项列表（不含任何健康/内部数据）。"""

    model_config = ConfigDict(extra="forbid")

    question_text: str
    options: list[ClarificationOptionView]
    inquiry_category: str | None = None


class ClarificationPrivateSnapshot(BaseModel):
    """问题私有快照：用于在用户选择后重构 QueryPlan/恢复历史约束（绝不对外公开）。"""

    model_config = ConfigDict(extra="ignore")

    query_plan_snapshot: dict[str, Any] | None = None
    option_modifications: dict[int, dict[str, Any]] = Field(default_factory=dict)
    current_menu_version: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class ClarificationView(BaseModel):
    """脱敏公开问题视图：用于 SSE 事件、GET 会话查询与前端渲染。"""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    question_text: str
    options: list[ClarificationOptionView]
    expires_at: float | str | None = None
    status: str = "pending"

    @classmethod
    def from_public_payload(
        cls,
        question_id: str,
        payload: ClarificationPublicPayload | dict[str, Any],
        expires_at: float | str | None = None,
        status: str = "pending",
    ) -> ClarificationView:
        if isinstance(payload, ClarificationPublicPayload):
            opts = [ClarificationOptionView(option_id=o.option_id, text=o.text) for o in payload.options]
            q_text = payload.question_text
        else:
            opts = [
                ClarificationOptionView(option_id=int(o["option_id"]), text=str(o["text"]))
                for o in payload.get("options", [])
            ]
            q_text = str(payload.get("question_text", ""))
        return cls(
            question_id=question_id,
            question_text=q_text,
            options=opts,
            expires_at=expires_at,
            status=status,
        )


class ClarificationResponseInput(BaseModel):
    """客户端 POST 请求中携带的结构化选择输入。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question_id: str = Field(..., min_length=1)
    option_id: int = Field(..., gt=0)


class ClarificationTransition(BaseModel):
    """Application 层原子提交中的澄清状态转移声明。

    在 MySQL `commit_request_result` 事务内完成旧问题消费、新问题创建与 active 指针切换。
    合法组合包括：
    - 首次追问：next_question_id + next_public_payload
    - 消费并完成：expected_question_id + selected_option_id
    - 消费并再次追问：expected_question_id + selected_option_id + next_question_id + next_public_payload
    - 替代当前问题：supersede_current=True (可选 next_question_id + next_public_payload)
    """

    model_config = ConfigDict(extra="forbid")

    expected_question_id: str | None = None
    selected_option_id: int | None = None
    expected_revision: int | None = None
    next_question_id: str | None = None
    next_public_payload: dict[str, Any] | None = None
    next_private_snapshot: dict[str, Any] | None = None
    next_expires_at: float | None = None
    supersede_current: bool = False

    from pydantic import model_validator

    @model_validator(mode="after")
    def validate_transition_combinations(self) -> ClarificationTransition:
        # 1. 选项消费必须指定待消费问题，且不能与替代标记共存
        if self.selected_option_id is not None:
            if not self.expected_question_id:
                raise ValueError("selected_option_id 必须指定 expected_question_id")
            if self.selected_option_id <= 0:
                raise ValueError("selected_option_id 必须为正整数")
            if self.supersede_current:
                raise ValueError("selected_option_id 与 supersede_current 互斥")

        if self.expected_question_id is not None:
            if self.selected_option_id is None:
                raise ValueError("消费 expected_question_id 时必须指定 selected_option_id")
            if self.supersede_current:
                raise ValueError("expected_question_id 与 supersede_current 互斥")

        # 2. 下一问题创建必须完整携带公开负载
        if self.next_question_id is not None:
            if not self.next_public_payload or not isinstance(self.next_public_payload, dict):
                raise ValueError("next_question_id 必须携带非空的 next_public_payload 字典")
            q_text = self.next_public_payload.get("question_text")
            options = self.next_public_payload.get("options")
            if not q_text or not isinstance(q_text, str) or not q_text.strip():
                raise ValueError("next_public_payload 必须包含非空 question_text")
            if not isinstance(options, list) or len(options) == 0:
                raise ValueError("next_public_payload 必须包含非空 options 列表")
            for opt in options:
                if isinstance(opt, dict) and "modifications" in opt:
                    raise ValueError("next_public_payload options 不得包含内部 modifications")
            if self.next_expires_at is not None and self.next_expires_at <= 0:
                raise ValueError("next_expires_at 必须为正数时间戳")
        else:
            if self.next_public_payload is not None:
                raise ValueError("未指定 next_question_id 时不得传入 next_public_payload")

        return self
