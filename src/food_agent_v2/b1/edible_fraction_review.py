"""Resolve reviewed edible fractions at occurrence or ingredient-form scope."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

ReviewStatus = Literal["pending", "approved", "modified", "rejected"]
_STATUSES = frozenset({"pending", "approved", "modified", "rejected"})
_ACTIVE = frozenset({"approved", "modified"})
_RULE_HEADERS = (
    "ingredient_id",
    "ingredient_name",
    "normalized_form",
    "edible_fraction",
    "review_status",
)
_DECISION_HEADERS = (
    "occurrence_id",
    "recipe_id",
    "ingredient_id",
    "ingredient_name",
    "normalized_form",
    "decision_form",
    "edible_fraction",
    "review_status",
)


@dataclass(frozen=True)
class EdibleFractionRule:
    ingredient_id: int
    ingredient_name: str
    normalized_form: str
    edible_fraction: Decimal
    review_status: ReviewStatus


class EdibleFractionRuleIndex:
    """Effective generic fractions keyed exclusively by ingredient ID and form."""

    def __init__(self, rules) -> None:
        self._effective: dict[tuple[int, str], EdibleFractionRule] = {}
        all_keys: set[tuple[int, str]] = set()
        forms_by_ingredient: dict[int, set[str]] = {}
        for rule in rules:
            _validate_rule(rule)
            key = (rule.ingredient_id, _form(rule.normalized_form))
            if key in all_keys:
                raise ValueError(f"重复可食比例规则: {key}")
            all_keys.add(key)
            forms_by_ingredient.setdefault(rule.ingredient_id, set()).add(key[1])
            if rule.review_status in _ACTIVE:
                self._effective[key] = rule
        if any("" in forms and len(forms) > 1 for forms in forms_by_ingredient.values()):
            raise ValueError("空白形态不得掩盖多形态事实")

    def get(self, ingredient_id: int, normalized_form: str) -> Decimal | None:
        rule = self.get_rule(ingredient_id, normalized_form)
        return rule.edible_fraction if rule is not None else None

    def get_rule(self, ingredient_id: int, normalized_form: str) -> EdibleFractionRule | None:
        return self._effective.get((ingredient_id, _form(normalized_form)))


@dataclass(frozen=True)
class EdibleFractionDecision:
    occurrence_id: str
    recipe_id: int
    ingredient_id: int
    ingredient_name: str
    normalized_form: str
    decision_form: str
    edible_fraction: Decimal
    review_status: ReviewStatus


class EdibleFractionDecisionIndex:
    """Only approved or modified occurrence decisions are queryable."""

    def __init__(self, decisions) -> None:
        self._effective: dict[str, EdibleFractionDecision] = {}
        for decision in decisions:
            _validate_decision(decision)
            if decision.review_status in _ACTIVE:
                if decision.occurrence_id in self._effective:
                    raise ValueError("同一 occurrence 存在重复有效可食比例决定")
                self._effective[decision.occurrence_id] = decision

    def get_effective(self, occurrence_id: str) -> EdibleFractionDecision | None:
        return self._effective.get(occurrence_id)


@dataclass(frozen=True)
class EdibleFractionResolution:
    edible_fraction: Decimal | None
    requires_review: bool


def resolve_edible_fraction(
    occurrence, rules: EdibleFractionRuleIndex, decisions: EdibleFractionDecisionIndex
) -> EdibleFractionResolution:
    """Prefer a matching occurrence decision and fail closed on any drift."""
    decision = decisions.get_effective(occurrence.occurrence_id)
    if decision is not None:
        if not _decision_matches(decision, occurrence):
            return EdibleFractionResolution(None, True)
        return EdibleFractionResolution(decision.edible_fraction, False)
    rule = rules.get_rule(occurrence.ingredient_id, occurrence.normalized_form)
    if rule is None or rule.ingredient_name != occurrence.ingredient_name:
        return EdibleFractionResolution(None, True)
    return EdibleFractionResolution(rule.edible_fraction, False)


def load_edible_fraction_rules(path: Path) -> EdibleFractionRuleIndex:
    return EdibleFractionRuleIndex(_read(path, _RULE_HEADERS, _parse_rule, "可食比例规则"))


def load_edible_fraction_decisions(path: Path) -> EdibleFractionDecisionIndex:
    return EdibleFractionDecisionIndex(
        _read(path, _DECISION_HEADERS, _parse_decision, "可食比例决定")
    )


def _decision_matches(decision: EdibleFractionDecision, occurrence) -> bool:
    return (
        decision.recipe_id == _recipe_id(occurrence.occurrence_id)
        and decision.ingredient_id == occurrence.ingredient_id
        and decision.ingredient_name == occurrence.ingredient_name
        and _form(decision.normalized_form) == _form(occurrence.normalized_form)
        and _form(decision.decision_form) == _form(occurrence.normalized_form)
    )


def _recipe_id(occurrence_id: str) -> int | None:
    prefix, separator, _ = occurrence_id.partition("-")
    return int(prefix) if separator and prefix.isdecimal() else None


def _validate_rule(rule: EdibleFractionRule) -> None:
    _positive_id(rule.ingredient_id, "可食比例规则 ingredient_id")
    _nonblank(rule.ingredient_name, "可食比例规则 ingredient_name")
    if not isinstance(rule.normalized_form, str):
        raise ValueError("可食比例规则 normalized_form 必须为字符串")
    _fraction(rule.edible_fraction)
    _status(rule.review_status, "可食比例规则")


def _validate_decision(decision: EdibleFractionDecision) -> None:
    _nonblank(decision.occurrence_id, "可食比例决定 occurrence_id")
    _positive_id(decision.recipe_id, "可食比例决定 recipe_id")
    _positive_id(decision.ingredient_id, "可食比例决定 ingredient_id")
    _nonblank(decision.ingredient_name, "可食比例决定 ingredient_name")
    if not isinstance(decision.normalized_form, str) or not isinstance(decision.decision_form, str):
        raise ValueError("可食比例决定形态必须为字符串")
    _fraction(decision.edible_fraction)
    _status(decision.review_status, "可食比例决定")


def _positive_id(value: int, label: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} 必须为正整数")


def _nonblank(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 不能为空")


def _fraction(value: Decimal) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not Decimal("0") < value <= Decimal("1")
    ):
        raise ValueError("可食比例必须在有限区间 (0, 1] 内")


def _status(value: str, label: str) -> None:
    if value not in _STATUSES:
        raise ValueError(f"{label} review_status 非法")


def _form(value: str) -> str:
    return value.strip()


def _read(path: Path, headers: tuple[str, ...], parser, label: str):
    review_path = Path(path)
    if not review_path.exists():
        return ()
    with review_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != headers:
            raise ValueError(f"{label} CSV 表头非法")
        return tuple(parser(row, line_number) for line_number, row in enumerate(reader, 2))


def _parse_rule(row: dict[str, str], line_number: int) -> EdibleFractionRule:
    try:
        return EdibleFractionRule(
            _int(row, "ingredient_id"),
            _text(row, "ingredient_name"),
            _form(row["normalized_form"]),
            _decimal(row, "edible_fraction"),
            _text(row, "review_status"),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"可食比例规则 CSV 第 {line_number} 行非法: {exc}") from exc


def _parse_decision(row: dict[str, str], line_number: int) -> EdibleFractionDecision:
    try:
        return EdibleFractionDecision(
            _text(row, "occurrence_id"),
            _int(row, "recipe_id"),
            _int(row, "ingredient_id"),
            _text(row, "ingredient_name"),
            _form(row["normalized_form"]),
            _form(row["decision_form"]),
            _decimal(row, "edible_fraction"),
            _text(row, "review_status"),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"可食比例决定 CSV 第 {line_number} 行非法: {exc}") from exc


def _text(row: dict[str, str], field: str) -> str:
    value = row[field]
    _nonblank(value, field)
    return value.strip()


def _int(row: dict[str, str], field: str) -> int:
    value = _text(row, field)
    if not value.isdecimal():
        raise ValueError(f"{field} 必须为正整数")
    return int(value)


def _decimal(row: dict[str, str], field: str) -> Decimal:
    try:
        return Decimal(_text(row, field))
    except InvalidOperation as exc:
        raise ValueError(f"{field} 必须为十进制数") from exc
