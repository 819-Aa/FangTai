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
_MAIN_CATEGORIES = frozenset({"肉禽", "水产", "谷物", "蛋奶", "豆制品", "水果"})
_SUPPORTING_CATEGORIES = frozenset({"蔬菜", "菌菇", "坚果"})
_COOKING_FAT_MARKERS = ("热锅", "下油", "炒", "煎", "炸", "爆")
_RETAINED_LIQUID_MARKERS = ("加入", "倒入", "煮", "炖", "焖", "烧", "煨")
_LIQUID_NAME_MARKERS = ("汤", "水", "汁")
_COOKING_FAT_NAME_MARKERS = ("油",)
_DECISION_FIELDS = (
    "occurrence_id",
    "usage_code",
    "retained_in_dish",
    "review_status",
)


@dataclass(frozen=True)
class NutritionUsageDecision:
    occurrence_id: str
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
    quantity_raw: str | None, step_text: str | None
) -> FuzzyTokenClass | None:
    """Map only explicit, closed fuzzy quantity expressions to stable classes."""
    material = f"{quantity_raw or ''}\n{step_text or ''}"
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
    if decision is not None:
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
    category = identity.category
    if category in _MAIN_CATEGORIES:
        return NutritionUsageResolution("main", True, False)
    if category in _SUPPORTING_CATEGORIES:
        return NutritionUsageResolution("supporting", True, False)
    if (
        any(marker in occurrence.name_clean for marker in _COOKING_FAT_NAME_MARKERS)
        and any(marker in bound_text for marker in _COOKING_FAT_MARKERS)
    ):
        return NutritionUsageResolution("cooking_fat", True, False)
    if (
        any(marker in occurrence.name_clean for marker in _LIQUID_NAME_MARKERS)
        and any(marker in bound_text for marker in _RETAINED_LIQUID_MARKERS)
    ):
        return NutritionUsageResolution("retained_liquid", True, False)
    if category == "调料":
        if any(marker in occurrence.name_clean for marker in _COOKING_FAT_NAME_MARKERS):
            return _requires_review()
        return NutritionUsageResolution("seasoning", True, False)
    return _requires_review()


def _requires_review() -> NutritionUsageResolution:
    return NutritionUsageResolution(None, None, True)


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
            usage_code=row["usage_code"].strip(),  # type: ignore[arg-type]
            retained_in_dish=retained == "true",
            review_status=row["review_status"].strip(),  # type: ignore[arg-type]
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"营养用途决定 CSV 第 {line_number} 行非法") from exc
