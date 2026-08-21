"""B4 健康规则引擎领域 Schema（T12）。

二元 PASS/EXCLUDE 只在固定关系与覆盖数据完整时产生；结果携带命中约束与证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class HealthIngredientOccurrence:
    ingredient_id: int
    condition_type: Literal["required", "optional", "one_of"]
    choice_group_id: str | None
    is_default_choice: bool
    is_process_material: bool


@dataclass
class RecipeHealthResult:
    """单道菜品对单个参与者的健康评估结果。"""

    recipe_id: int
    participant_ref: str
    verdict: str  # PASS | EXCLUDE
    hitting_constraints: list[dict] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class HealthEvaluationReceipt:
    """候选批次健康审查回执。"""

    evaluation_id: str
    request_id: str | None
    retrieval_result_ref: str | None
    constraint_set_refs: list[str]
    participant_recipe_results: list[RecipeHealthResult]
    safe_recipe_ids: list[int]
    excluded_recipe_ids: list[int]
    input_fingerprint: str
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class FinalValidationResult:
    """最终健康校验结果（绑定 plan_id 与 menu_hash）。"""

    request_id: str
    plan_id: str
    verdict: str  # PASS | EXCLUDE
    menu_hash: str
    participant_recipe_results: list[RecipeHealthResult]
    evidence_refs: list[str] = field(default_factory=list)
