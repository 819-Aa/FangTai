"""冻结营养输入中每次食材出现的用途和留存语义。"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from food_agent_v2.b1.time_review_decisions import ReviewStatus

if TYPE_CHECKING:
    from food_agent_v2.b1.consumer_views import (
        IngredientIdentityFact,
        IngredientOccurrenceFact,
        StructuredStep,
    )


UsageCode = Literal["main", "supporting", "seasoning", "cooking_fat", "retained_liquid"]
FuzzyTokenClass = Literal["as_needed", "small_amount", "several_count", "few_drops"]

_USAGE_CODES = frozenset({"main", "supporting", "seasoning", "cooking_fat", "retained_liquid"})
_REVIEW_STATUSES = frozenset({"pending", "approved", "modified", "rejected"})
_ACTIVE_REVIEW_STATUSES = frozenset({"approved", "modified"})
_FUZZY_TOKENS: tuple[tuple[str, FuzzyTokenClass], ...] = (
    ("适量", "as_needed"),
    ("少许", "small_amount"),
    ("数个", "several_count"),
    ("若干", "several_count"),
    ("几滴", "few_drops"),
)
_COOKING_FAT_MARKERS = ("热锅", "下油", "炒", "煎", "炸", "爆")
_RETAINED_LIQUID_MARKERS = ("加入", "倒入", "煮", "炖", "焖", "烧", "煨")
_DISCARDED_LIQUID_MARKERS = ("焯", "浸泡", "沥", "过滤", "滤", "倒掉", "弃", "捞出")
_LIQUID_NAME_MARKERS = ("汤", "水", "汁")
_COOKING_OIL_IDENTITIES = frozenset({
    "食用油", "植物油", "花生油", "菜籽油", "橄榄油", "玉米油", "大豆油", "葵花籽油", "猪油", "色拉油",
})
_DECISION_FIELDS = (
    "occurrence_id",
    "recipe_id",
    "ingredient_id",
    "ingredient_name",
    "normalized_form",
    "usage_code",
    "retained_in_dish",
    "review_status",
)


@dataclass(frozen=True)
class NutritionUsageDecision:
    occurrence_id: str
    recipe_id: int
    ingredient_id: int
    ingredient_name: str
    normalized_form: str
    usage_code: UsageCode
    retained_in_dish: bool
    review_status: ReviewStatus


class NutritionUsageDecisionIndex:
    """Occurrence-level decisions; only approved/modified rows are effective."""

    def __init__(self, decisions: tuple[NutritionUsageDecision, ...]) -> None:
        effective_by_occurrence: dict[str, NutritionUsageDecision] = {}
        for decision in decisions:
            if not decision.occurrence_id:
                raise ValueError("occurrence_id 不能为空")
            if decision.recipe_id < 1 or decision.ingredient_id < 1:
                raise ValueError("recipe_id/ingredient_id 必须为正整数")
            if not decision.ingredient_name.strip():
                raise ValueError("ingredient_name 不能为空")
            if decision.usage_code not in _USAGE_CODES:
                raise ValueError("usage_code 越出封闭词表")
            if decision.review_status not in _REVIEW_STATUSES:
                raise ValueError("review_status 越出封闭词表")
            if decision.review_status in _ACTIVE_REVIEW_STATUSES:
                if decision.occurrence_id in effective_by_occurrence:
                    raise ValueError("同一 occurrence 存在重复有效营养用途决定")
                effective_by_occurrence[decision.occurrence_id] = decision
        self._effective_by_occurrence = effective_by_occurrence

    def get_effective(self, occurrence_id: str) -> NutritionUsageDecision | None:
        return self._effective_by_occurrence.get(occurrence_id)


def load_nutrition_usage_decisions(path: Path) -> NutritionUsageDecisionIndex:
    """Load the strict occurrence decision sheet; inactive rows remain auditable."""
    decision_path = Path(path)
    if not decision_path.exists():
        return NutritionUsageDecisionIndex(())
    with decision_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _DECISION_FIELDS:
            raise ValueError("营养用途决定 CSV 表头非法")
        decisions = tuple(
            _parse_decision_row(row, line_number)
            for line_number, row in enumerate(reader, start=2)
        )
    return NutritionUsageDecisionIndex(decisions)


@dataclass(frozen=True)
class NutritionUsageResolution:
    usage_code: UsageCode | None
    retained_in_dish: bool | None
    requires_review: bool


def classify_fuzzy_token(
    quantity_raw: str | None, occurrence_evidence: str | None
) -> FuzzyTokenClass | None:
    """Map only occurrence-local fuzzy evidence; an explicit number wins."""
    raw = quantity_raw or ""
    if any(character.isdigit() for character in raw):
        return None
    material = f"{raw}\n{occurrence_evidence or ''}"
    for token, token_class in _FUZZY_TOKENS:
        if token in material:
            return token_class
    return None


def derive_nutrition_usage(
    *,
    occurrence: IngredientOccurrenceFact,
    identity: IngredientIdentityFact,
    steps: tuple[StructuredStep, ...],
    decisions: NutritionUsageDecisionIndex,
) -> NutritionUsageResolution:
    """Resolve from an effective decision or unambiguous category/bound-step evidence."""
    decision = decisions.get_effective(occurrence.occurrence_id)
    if decision is not None and _decision_matches(decision, occurrence, identity):
        return NutritionUsageResolution(
            usage_code=decision.usage_code,
            retained_in_dish=decision.retained_in_dish,
            requires_review=False,
        )

    bound_text = "\n".join(
        step.raw_text
        for step in steps
        if occurrence.occurrence_id in step.bound_occurrence_ids
    )
    if (
        identity.name_canonical in _COOKING_OIL_IDENTITIES
        and occurrence.name_clean == identity.name_canonical
        and any(marker in bound_text for marker in _COOKING_FAT_MARKERS)
    ):
        return NutritionUsageResolution("cooking_fat", True, False)
    if (
        any(marker in occurrence.name_clean for marker in _LIQUID_NAME_MARKERS)
    ):
        if any(marker in bound_text for marker in _DISCARDED_LIQUID_MARKERS):
            return NutritionUsageResolution("retained_liquid", False, False)
        if any(marker in bound_text for marker in _RETAINED_LIQUID_MARKERS):
            return NutritionUsageResolution("retained_liquid", True, False)
        return _requires_review()
    if identity.category == "调料":
        return NutritionUsageResolution("seasoning", True, False)
    return _requires_review()


def _requires_review() -> NutritionUsageResolution:
    return NutritionUsageResolution(None, None, True)


def _decision_matches(
    decision: NutritionUsageDecision,
    occurrence: IngredientOccurrenceFact,
    identity: IngredientIdentityFact,
) -> bool:
    return (
        decision.recipe_id == occurrence.recipe_id
        and decision.ingredient_id == occurrence.ingredient_id
        and decision.ingredient_name == identity.name_canonical
        and decision.normalized_form == (occurrence.form or "").strip()
    )


def _parse_decision_row(
    row: dict[str, str], line_number: int
) -> NutritionUsageDecision:
    try:
        if set(row) != set(_DECISION_FIELDS):
            raise ValueError("字段不完整或含额外字段")
        retained = row["retained_in_dish"].strip().lower()
        if retained not in {"true", "false"}:
            raise ValueError("retained_in_dish 必须为 true 或 false")
        return NutritionUsageDecision(
            occurrence_id=row["occurrence_id"].strip(),
            recipe_id=int(row["recipe_id"]),
            ingredient_id=int(row["ingredient_id"]),
            ingredient_name=row["ingredient_name"].strip(),
            normalized_form=row["normalized_form"].strip(),
            usage_code=row["usage_code"].strip(),  # type: ignore[arg-type]
            retained_in_dish=retained == "true",
            review_status=row["review_status"].strip(),  # type: ignore[arg-type]
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"营养用途决定 CSV 第 {line_number} 行非法: {exc}") from exc
