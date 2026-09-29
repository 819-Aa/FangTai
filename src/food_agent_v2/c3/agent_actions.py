"""Agent 强类型行动、参数 Schema、观察结果与澄清契约（Section 5）。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionType(StrEnum):
    READ_MENU = "read_menu"
    SEARCH_CANDIDATES = "search_candidates"
    EXPAND_CANDIDATES = "expand_candidates"
    AUDIT_RECIPE_HEALTH = "audit_recipe_health"
    COMBINE_NUTRITIONAL_MENU = "combine_nutritional_menu"
    VALIDATE_SELECTED_MENU = "validate_selected_menu"
    ASK_USER = "ask_user"
    FINISH = "finish"


class BaseActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadMenuArgs(BaseActionArgs):
    target: Literal["current", "previous_committed"] = "current"


class SearchCandidatesArgs(BaseActionArgs):
    query: str
    meal_type: str | None = None


class ExpandCandidatesArgs(BaseActionArgs):
    query: str
    retrieval_evidence_ref: str | None = None


class AuditRecipeHealthArgs(BaseActionArgs):
    candidate_recipe_ids: list[int] = Field(default_factory=list)


class CombineNutritionalMenuArgs(BaseActionArgs):
    health_receipt_ref: str | None = None
    dish_count: int | None = None


class ValidateSelectedMenuArgs(BaseActionArgs):
    plan_id: str
    recipe_ids: list[int] = Field(default_factory=list)
    feasible_menu_ref: str | None = None


class AskUserArgs(BaseActionArgs):
    inquiry_category: str = "constraint"
    reason: str = ""
    options: list[dict[str, Any]] = Field(default_factory=list)


class FinishArgs(BaseActionArgs):
    plan_id: str
    final_validation_ref: str


class AgentAction(BaseModel):
    """Agent 类型化行动信封。禁止额外字段，公共字段包含 action, arguments, evidence_refs, summary。"""

    model_config = ConfigDict(extra="forbid")

    action: ActionType
    arguments: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    summary: str = ""


class Observation(BaseModel):
    """脱敏观察结果，用于向 Agent 反馈工具事实。"""

    model_config = ConfigDict(extra="ignore")

    action: ActionType
    status: Literal["ok", "no_solution", "error"]
    desensitized_facts: dict[str, Any] = Field(default_factory=dict)
    evidence_ref: str | None = None
    error_code: str | None = None
    message: str = ""
