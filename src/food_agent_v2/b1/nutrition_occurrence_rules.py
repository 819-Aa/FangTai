"""Freeze per-occurrence nutrition usage and retention decisions separately."""

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


UsageCode = Literal[
    "main", "supporting", "seasoning", "cooking_fat", "cooking_liquid"
]
FuzzyTokenClass = Literal["as_needed", "small_amount", "several_count", "few_drops"]

_USAGE_CODES = frozenset({"main", "supporting", "seasoning", "cooking_fat", "cooking_liquid"})
_REVIEW_STATUSES = frozenset({"pending", "approved", "modified", "rejected"})
_ACTIVE_REVIEW_STATUSES = frozenset({"approved", "modified"})
_LIQUID_NAME_MARKERS = ("汤", "水", "汁")
_COOKING_OIL_IDENTITIES = frozenset(
    {
        "油",
        "食用油",
        "植物油",
        "花生油",
        "菜籽油",
        "橄榄油",
        "玉米油",
        "大豆油",
        "葵花籽油",
        "猪油",
        "色拉油",
    }
)
_FUZZY_TOKENS: tuple[tuple[str, FuzzyTokenClass], ...] = (
    ("适量", "as_needed"),
    ("少许", "small_amount"),
    ("数个", "several_count"),
    ("若干", "several_count"),
    ("几滴", "few_drops"),
)
_USAGE_DECISION_FIELDS = (
    "occurrence_id",
    "recipe_id",
    "ingredient_id",
    "ingredient_name",
    "normalized_form",
    "usage_code",
    "review_status",
)
_RETENTION_DECISION_FIELDS = (
    "occurrence_id",
    "recipe_id",
    "ingredient_id",
    "ingredient_name",
    "normalized_form",
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
    review_status: ReviewStatus


@dataclass(frozen=True)
class NutritionRetentionDecision:
    occurrence_id: str
    recipe_id: int
    ingredient_id: int
    ingredient_name: str
    normalized_form: str
    retained_in_dish: bool
    review_status: ReviewStatus


@dataclass(frozen=True)
class NutritionUsageResolution:
    usage_code: UsageCode | None
    requires_review: bool


@dataclass(frozen=True)
class NutritionRetentionResolution:
    retained_in_dish: bool | None
    requires_review: bool


class NutritionUsageDecisionIndex:
    """Usage decisions whose approved/modified rows are effective."""

    def __init__(self, decisions: tuple[NutritionUsageDecision, ...]) -> None:
        self._effective_by_occurrence = _effective_decisions(
            decisions, kind="营养用途", validate=_validate_usage_decision
        )

    def get_effective(self, occurrence_id: str) -> NutritionUsageDecision | None:
        return self._effective_by_occurrence.get(occurrence_id)


class NutritionRetentionDecisionIndex:
    """Retention decisions whose approved/modified rows are effective."""

    def __init__(self, decisions: tuple[NutritionRetentionDecision, ...]) -> None:
        self._effective_by_occurrence = _effective_decisions(
            decisions, kind="营养留存", validate=_validate_retention_decision
        )

    def get_effective(self, occurrence_id: str) -> NutritionRetentionDecision | None:
        return self._effective_by_occurrence.get(occurrence_id)


def load_nutrition_usage_decisions(
    path: Path, *, current_occurrence_ids: set[str] | None = None
) -> NutritionUsageDecisionIndex:
    """Load the strict usage decision sheet, retaining inactive rows for audit."""
    decisions = _load_decisions(
        path,
        fields=_USAGE_DECISION_FIELDS,
        kind="营养用途",
        parser=_parse_usage_decision_row,
    )
    _ensure_known_occurrences(decisions, current_occurrence_ids)
    return NutritionUsageDecisionIndex(decisions)


def load_nutrition_retention_decisions(
    path: Path, *, current_occurrence_ids: set[str] | None = None
) -> NutritionRetentionDecisionIndex:
    """Load the strict retention decision sheet, retaining inactive rows for audit."""
    decisions = _load_decisions(
        path,
        fields=_RETENTION_DECISION_FIELDS,
        kind="营养留存",
        parser=_parse_retention_decision_row,
    )
    _ensure_known_occurrences(decisions, current_occurrence_ids)
    return NutritionRetentionDecisionIndex(decisions)


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
    """Resolve usage only from a reviewed decision or the seasoning category."""
    del steps
    decision = decisions.get_effective(occurrence.occurrence_id)
    if decision is not None:
        if not _decision_matches(decision, occurrence, identity):
            return NutritionUsageResolution(None, True)
        return NutritionUsageResolution(decision.usage_code, False)
    if identity.name_canonical in _COOKING_OIL_IDENTITIES:
        return NutritionUsageResolution(None, True)
    if any(marker in occurrence.name_clean for marker in _LIQUID_NAME_MARKERS):
        return NutritionUsageResolution(None, True)
    if identity.category == "调料":
        return NutritionUsageResolution("seasoning", False)
    return NutritionUsageResolution(None, True)


def derive_nutrition_retention(
    *,
    occurrence: IngredientOccurrenceFact,
    identity: IngredientIdentityFact,
    steps: tuple[StructuredStep, ...],
    decisions: NutritionRetentionDecisionIndex,
) -> NutritionRetentionResolution:
    """Retention always needs an explicit, metadata-matching reviewed decision."""
    del steps
    decision = decisions.get_effective(occurrence.occurrence_id)
    if decision is None or not _decision_matches(decision, occurrence, identity):
        return NutritionRetentionResolution(None, True)
    return NutritionRetentionResolution(decision.retained_in_dish, False)


def _effective_decisions(decisions, *, kind: str, validate):
    effective_by_occurrence = {}
    seen_occurrence_ids: set[str] = set()
    for decision in decisions:
        validate(decision)
        if decision.occurrence_id in seen_occurrence_ids:
            raise ValueError(f"同一 occurrence 存在重复{kind}决定")
        seen_occurrence_ids.add(decision.occurrence_id)
        if decision.review_status in _ACTIVE_REVIEW_STATUSES:
            effective_by_occurrence[decision.occurrence_id] = decision
    return effective_by_occurrence


def _validate_common_decision(decision) -> None:
    if not decision.occurrence_id:
        raise ValueError("occurrence_id 不能为空")
    if decision.recipe_id < 1 or decision.ingredient_id < 1:
        raise ValueError("recipe_id/ingredient_id 必须为正整数")
    if not decision.ingredient_name.strip():
        raise ValueError("ingredient_name 不能为空")
    if decision.review_status not in _REVIEW_STATUSES:
        raise ValueError("review_status 越出封闭词表")


def _validate_usage_decision(decision: NutritionUsageDecision) -> None:
    _validate_common_decision(decision)
    if decision.usage_code not in _USAGE_CODES:
        raise ValueError("usage_code 越出封闭词表")


def _validate_retention_decision(decision: NutritionRetentionDecision) -> None:
    _validate_common_decision(decision)
    if not isinstance(decision.retained_in_dish, bool):
        raise ValueError("retained_in_dish 必须为 bool")


def _load_decisions(path: Path, *, fields, kind: str, parser):
    decision_path = Path(path)
    if not decision_path.exists():
        return ()
    with decision_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError(f"{kind}决定 CSV 表头非法")
        return tuple(parser(row, line_number) for line_number, row in enumerate(reader, start=2))


def _ensure_known_occurrences(decisions, current_occurrence_ids: set[str] | None) -> None:
    if current_occurrence_ids is None:
        return
    unknown = sorted(
        decision.occurrence_id
        for decision in decisions
        if decision.occurrence_id not in current_occurrence_ids
    )
    if unknown:
        raise ValueError(f"未知 occurrence_id: {unknown[:5]}")


def _decision_matches(decision, occurrence, identity) -> bool:
    return (
        decision.recipe_id == occurrence.recipe_id
        and decision.ingredient_id == occurrence.ingredient_id
        and decision.ingredient_name == identity.name_canonical
        and decision.normalized_form == (occurrence.form or "").strip()
    )


def _parse_usage_decision_row(row: dict[str, str], line_number: int) -> NutritionUsageDecision:
    try:
        _ensure_fields(row, _USAGE_DECISION_FIELDS)
        return NutritionUsageDecision(
            occurrence_id=row["occurrence_id"].strip(),
            recipe_id=int(row["recipe_id"]),
            ingredient_id=int(row["ingredient_id"]),
            ingredient_name=row["ingredient_name"].strip(),
            normalized_form=row["normalized_form"].strip(),
            usage_code=row["usage_code"].strip(),  # type: ignore[arg-type]
            review_status=row["review_status"].strip(),  # type: ignore[arg-type]
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"营养用途决定 CSV 第 {line_number} 行非法: {exc}") from exc


def _parse_retention_decision_row(
    row: dict[str, str], line_number: int
) -> NutritionRetentionDecision:
    try:
        _ensure_fields(row, _RETENTION_DECISION_FIELDS)
        retained = row["retained_in_dish"].strip().lower()
        if retained not in {"true", "false"}:
            raise ValueError("retained_in_dish 必须为 true 或 false")
        return NutritionRetentionDecision(
            occurrence_id=row["occurrence_id"].strip(),
            recipe_id=int(row["recipe_id"]),
            ingredient_id=int(row["ingredient_id"]),
            ingredient_name=row["ingredient_name"].strip(),
            normalized_form=row["normalized_form"].strip(),
            retained_in_dish=retained == "true",
            review_status=row["review_status"].strip(),  # type: ignore[arg-type]
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"营养留存决定 CSV 第 {line_number} 行非法: {exc}") from exc


def _ensure_fields(row: dict[str, str], fields: tuple[str, ...]) -> None:
    if set(row) != set(fields):
        raise ValueError("字段不完整或含额外字段")
