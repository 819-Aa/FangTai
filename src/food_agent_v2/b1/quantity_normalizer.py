"""确定性地将营养 occurrence 用量规范为克。"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from food_agent_v2.b1.nutrition_occurrence_rules import FuzzyTokenClass, UsageCode

ReviewStatus = Literal["pending", "approved", "modified", "rejected"]
_REVIEW_STATUSES = frozenset({"pending", "approved", "modified", "rejected"})
_USAGE_CODES = frozenset(
    {"main", "supporting", "seasoning", "cooking_fat", "retained_liquid"}
)
_FUZZY_TOKEN_CLASSES = frozenset(
    {"as_needed", "small_amount", "several_count", "few_drops"}
)
_COUNT_UNITS = frozenset({"个", "片", "根", "勺"})

_MEASURE_HEADERS = (
    "schema_version",
    "rule_id",
    "rule_type",
    "ingredient_id",
    "ingredient_name",
    "normalized_form",
    "normalized_unit",
    "usage_code",
    "fuzzy_token_class",
    "to_grams",
    "mass_density_g_per_ml",
    "review_status",
)
_EDIBLE_FRACTION_HEADERS = (
    "ingredient_name",
    "form",
    "edible_fraction",
    "review_status",
)
_QUANTITY_DECISION_HEADERS = (
    "occurrence_id",
    "recipe_id",
    "recipe_name",
    "ingredient_name",
    "raw_quantity",
    "step_context",
    "deterministic_calculation",
    "candidate_basis",
    "candidate_grams",
    "decision_grams",
    "review_status",
)


@dataclass(frozen=True)
class MeasureRule:
    schema_version: Literal["2.0.0"]
    rule_id: str
    rule_type: Literal["unit_weight", "density", "fuzzy_single_value"]
    ingredient_id: int
    ingredient_name: str
    normalized_form: str
    normalized_unit: str | None
    usage_code: UsageCode | None
    fuzzy_token_class: FuzzyTokenClass | None
    to_grams: Decimal | None
    mass_density_g_per_ml: Decimal | None
    review_status: ReviewStatus


class MeasureRuleIndex:
    """Indexes approved V2 rules at their only valid matching granularity."""

    def __init__(self, rules, *, _allow_inactive: bool = False) -> None:
        self._unit_weights: dict[tuple[int, str, str], MeasureRule] = {}
        self._densities: dict[tuple[int, str], MeasureRule] = {}
        self._fuzzy_single_values: dict[
            tuple[int, str, UsageCode, FuzzyTokenClass], MeasureRule
        ] = {}
        rule_ids: set[str] = set()
        for rule in rules:
            self._validate_common(rule, rule_ids)
            if rule.rule_type == "unit_weight":
                self._add_unit_weight(rule)
            elif rule.rule_type == "density":
                self._add_density(rule)
            elif rule.rule_type == "fuzzy_single_value":
                self._add_fuzzy_single_value(rule)
            else:
                raise ValueError(f"不支持的 rule_type: {rule.rule_type}")
            if rule.review_status != "approved" and not _allow_inactive:
                raise ValueError(f"计量规则未批准: {rule.rule_id}")

    def get_unit_weight(
        self, ingredient_id: int, normalized_form: str, normalized_unit: str
    ) -> MeasureRule | None:
        return self._unit_weights.get(
            (ingredient_id, _normalized_form(normalized_form), _normalize_unit(normalized_unit))
        )

    def get_density(
        self, ingredient_id: int, normalized_form: str
    ) -> MeasureRule | None:
        return self._densities.get((ingredient_id, _normalized_form(normalized_form)))

    def get_fuzzy_single_value(
        self,
        ingredient_id: int,
        normalized_form: str,
        usage_code: UsageCode,
        fuzzy_token_class: FuzzyTokenClass,
    ) -> MeasureRule | None:
        return self._fuzzy_single_values.get(
            (
                ingredient_id,
                _normalized_form(normalized_form),
                usage_code,
                fuzzy_token_class,
            )
        )

    def _validate_common(self, rule: MeasureRule, rule_ids: set[str]) -> None:
        if rule.schema_version != "2.0.0":
            raise ValueError(f"计量规则 schema_version 非法: {rule.rule_id}")
        if (
            not isinstance(rule.rule_id, str)
            or not rule.rule_id
            or rule.rule_id != rule.rule_id.strip()
            or rule.rule_id in rule_ids
        ):
            raise ValueError(f"重复或为空的计量规则 rule_id: {rule.rule_id}")
        rule_ids.add(rule.rule_id)
        if type(rule.ingredient_id) is not int or rule.ingredient_id < 1:
            raise ValueError(f"计量规则 ingredient_id 必须为正整数: {rule.rule_id}")
        if not rule.ingredient_name.strip():
            raise ValueError(f"计量规则 ingredient_name 不能为空: {rule.rule_id}")
        if rule.review_status not in _REVIEW_STATUSES:
            raise ValueError(f"计量规则 review_status 非法: {rule.rule_id}")

    def _add_unit_weight(self, rule: MeasureRule) -> None:
        unit = _normalize_unit(rule.normalized_unit or "")
        if unit not in _COUNT_UNITS:
            raise ValueError(f"单位重量规则单位非法: {rule.rule_id}")
        if not _is_finite_positive_decimal(rule.to_grams):
            raise ValueError(f"单位重量规则无合法克重: {rule.rule_id}")
        if any(
            value is not None
            for value in (
                rule.mass_density_g_per_ml,
                rule.usage_code,
                rule.fuzzy_token_class,
            )
        ):
            raise ValueError(f"单位重量规则字段不匹配: {rule.rule_id}")
        key = (rule.ingredient_id, _normalized_form(rule.normalized_form), unit)
        if key in self._unit_weights:
            raise ValueError(f"重复计量规则: {key}")
        self._unit_weights[key] = rule

    def _add_density(self, rule: MeasureRule) -> None:
        if _normalize_unit(rule.normalized_unit or "") != "毫升":
            raise ValueError(f"密度规则单位必须为毫升: {rule.rule_id}")
        if not _is_finite_positive_decimal(rule.mass_density_g_per_ml):
            raise ValueError(f"密度规则无合法密度: {rule.rule_id}")
        if any(
            value is not None
            for value in (rule.to_grams, rule.usage_code, rule.fuzzy_token_class)
        ):
            raise ValueError(f"密度规则字段不匹配: {rule.rule_id}")
        key = (rule.ingredient_id, _normalized_form(rule.normalized_form))
        if key in self._densities:
            raise ValueError(f"重复计量规则: {key}")
        self._densities[key] = rule

    def _add_fuzzy_single_value(self, rule: MeasureRule) -> None:
        if rule.normalized_unit is not None and rule.normalized_unit.strip():
            raise ValueError(f"模糊单值规则不得包含单位: {rule.rule_id}")
        if not _is_finite_positive_decimal(rule.to_grams):
            raise ValueError(f"模糊单值规则无合法克重: {rule.rule_id}")
        if rule.mass_density_g_per_ml is not None:
            raise ValueError(f"模糊单值规则不得包含密度: {rule.rule_id}")
        if rule.usage_code not in _USAGE_CODES:
            raise ValueError(f"模糊单值规则 usage_code 非法: {rule.rule_id}")
        if rule.fuzzy_token_class not in _FUZZY_TOKEN_CLASSES:
            raise ValueError(f"模糊单值规则 fuzzy_token_class 非法: {rule.rule_id}")
        key = (
            rule.ingredient_id,
            _normalized_form(rule.normalized_form),
            rule.usage_code,
            rule.fuzzy_token_class,
        )
        if key in self._fuzzy_single_values:
            raise ValueError(f"重复计量规则: {key}")
        self._fuzzy_single_values[key] = rule


@dataclass(frozen=True)
class QuantityDecision:
    occurrence_id: str
    decision_grams: Decimal
    review_status: ReviewStatus


class QuantityDecisionIndex:
    def __init__(self, decisions) -> None:
        self._decisions: dict[str, QuantityDecision] = {}
        for decision in decisions:
            if decision.review_status not in _REVIEW_STATUSES:
                raise ValueError(f"用量决定 review_status 非法: {decision.occurrence_id}")
            if decision.occurrence_id in self._decisions:
                raise ValueError(f"重复用量决定: {decision.occurrence_id}")
            if not _is_finite_positive_decimal(decision.decision_grams):
                raise ValueError(f"用量决定必须大于零: {decision.occurrence_id}")
            self._decisions[decision.occurrence_id] = decision

    def get_effective(self, occurrence_id: str) -> QuantityDecision | None:
        decision = self._decisions.get(occurrence_id)
        if decision and decision.review_status in ("approved", "modified"):
            return decision
        return None


@dataclass(frozen=True)
class EdibleFractionRule:
    ingredient_name: str
    form: str
    edible_fraction: Decimal
    review_status: ReviewStatus


class EdibleFractionRuleIndex:
    def __init__(self, rules) -> None:
        self._rules: dict[tuple[str, str], Decimal] = {}
        for rule in rules:
            if rule.review_status not in _REVIEW_STATUSES:
                raise ValueError(
                    f"可食比例规则 review_status 非法: {rule.ingredient_name}/{rule.form}"
                )
            if rule.review_status != "approved":
                raise ValueError(
                    f"可食比例规则未批准: {rule.ingredient_name}/{rule.form}"
                )
            if not Decimal("0") < rule.edible_fraction <= Decimal("1"):
                raise ValueError(
                    f"可食比例必须在 (0, 1]：{rule.ingredient_name}/{rule.form}"
                )
            key = (rule.ingredient_name.strip(), (rule.form or "").strip())
            if key in self._rules:
                raise ValueError(f"重复可食比例规则: {key}")
            self._rules[key] = rule.edible_fraction

    def get(self, ingredient_name: str, form: str | None) -> Decimal | None:
        return self._rules.get((ingredient_name.strip(), (form or "").strip()))


def load_measure_rules(path: Path) -> MeasureRuleIndex:
    rules: list[MeasureRule] = []
    for row in _read_review_csv(path, _MEASURE_HEADERS):
        status = _review_status(row, path)
        rule_type = _required(row, "rule_type")
        if rule_type not in {"unit_weight", "density", "fuzzy_single_value"}:
            raise ValueError(f"不支持的 rule_type: {rule_type}")
        rules.append(
            MeasureRule(
                schema_version=_required(row, "schema_version"),  # type: ignore[arg-type]
                rule_id=_required(row, "rule_id"),
                rule_type=rule_type,  # type: ignore[arg-type]
                ingredient_id=_required_int(row, "ingredient_id"),
                ingredient_name=_required(row, "ingredient_name"),
                normalized_form=(row["normalized_form"] or "").strip(),
                normalized_unit=_optional_text(row, "normalized_unit"),
                usage_code=_optional_text(row, "usage_code"),  # type: ignore[arg-type]
                fuzzy_token_class=_optional_text(row, "fuzzy_token_class"),  # type: ignore[arg-type]
                to_grams=_optional_decimal(row, "to_grams"),
                mass_density_g_per_ml=_optional_decimal(
                    row, "mass_density_g_per_ml"
                ),
                review_status=status,
            )
        )
    MeasureRuleIndex(rules, _allow_inactive=True)
    return MeasureRuleIndex(rule for rule in rules if rule.review_status == "approved")


def load_quantity_decisions(path: Path) -> QuantityDecisionIndex:
    decisions: list[QuantityDecision] = []
    for row in _read_review_csv(path, _QUANTITY_DECISION_HEADERS):
        status = _review_status(row, path)
        if status not in {"approved", "modified"}:
            continue
        decisions.append(
            QuantityDecision(
                occurrence_id=_required(row, "occurrence_id"),
                decision_grams=_required_decimal(row, "decision_grams"),
                review_status=status,
            )
        )
    return QuantityDecisionIndex(decisions)


def load_edible_fraction_rules(path: Path) -> EdibleFractionRuleIndex:
    rules: list[EdibleFractionRule] = []
    for row in _read_review_csv(path, _EDIBLE_FRACTION_HEADERS):
        status = _review_status(row, path)
        if status != "approved":
            continue
        rules.append(
            EdibleFractionRule(
                ingredient_name=_required(row, "ingredient_name"),
                form=row["form"].strip(),
                edible_fraction=_required_decimal(row, "edible_fraction"),
                review_status="approved",
            )
        )
    return EdibleFractionRuleIndex(rules)


@dataclass(frozen=True)
class QuantityNormalizationResult:
    standardized_grams: Decimal | None
    requires_review: bool
    review_status: ReviewStatus | None


def normalize_quantity(occurrence, measure_rules, decisions) -> QuantityNormalizationResult:
    """Apply the V2 priority order without broadening a rule's scope."""
    if (
        occurrence.requires_review
        or occurrence.usage_code is None
        or occurrence.retained_in_dish is None
    ):
        return _pending()
    decision = decisions.get_effective(occurrence.occurrence_id)
    if decision is not None:
        return QuantityNormalizationResult(
            standardized_grams=decision.decision_grams,
            requires_review=False,
            review_status=decision.review_status,
        )

    raw = (occurrence.quantity_raw or "").strip()
    parsed = _parse_deterministic_quantity(raw)
    amount, unit = parsed if parsed is not None else (None, "")
    input_unit = _normalize_unit(occurrence.unit_raw or "")
    if input_unit and unit and input_unit != unit:
        return _pending()
    if amount is not None and unit in _MASS_FACTORS:
        return _resolved(amount * _MASS_FACTORS[unit])

    normalized_form = _normalized_form(occurrence.normalized_form)
    if amount is not None and unit in _COUNT_UNITS:
        rule = measure_rules.get_unit_weight(
            occurrence.ingredient_id, normalized_form, unit
        )
        if rule is not None:
            return _resolved(amount * rule.to_grams)

    if amount is not None and unit in {"毫升", "升"}:
        rule = measure_rules.get_density(occurrence.ingredient_id, normalized_form)
        if rule is not None:
            volume_ml = amount * (Decimal("1000") if unit == "升" else Decimal("1"))
            return _resolved(volume_ml * rule.mass_density_g_per_ml)

    usage_code = occurrence.usage_code
    fuzzy_token_class = occurrence.fuzzy_token_class
    if usage_code is not None and fuzzy_token_class is not None:
        rule = measure_rules.get_fuzzy_single_value(
            occurrence.ingredient_id,
            normalized_form,
            usage_code,
            fuzzy_token_class,
        )
        if rule is not None:
            return _resolved(rule.to_grams)
    return _pending()


_MASS_FACTORS = {
    "克": Decimal("1"),
    "千克": Decimal("1000"),
    "斤": Decimal("500"),
    "两": Decimal("50"),
}


def _is_finite_positive_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _resolved(grams: Decimal | None) -> QuantityNormalizationResult:
    if grams is None:
        raise ValueError("有效规则缺少克重")
    return QuantityNormalizationResult(grams, False, None)


def _pending() -> QuantityNormalizationResult:
    return QuantityNormalizationResult(None, True, "pending")


def _normalized_form(form: str | None) -> str:
    return (form or "").strip()


def _normalize_unit(unit: str) -> str:
    normalized = (unit or "").strip().lower()
    return {
        "g": "克",
        "kg": "千克",
        "公斤": "千克",
        "ml": "毫升",
        "l": "升",
    }.get(normalized, normalized)


_QUANTITY_EXPRESSION = re.compile(
    r"^(?:大约|约)?"
    r"(?P<first>\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?|半|一|二|两)"
    r"(?:(?:-|–|—|~|～|至|到)(?P<second>\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?))?"
    r"(?P<unit>千克|公斤|毫升|茶匙|汤匙|kg|ml|克|斤|两|升|g|l|个|片|根|勺)"
    r"(?:左右)?$",
    re.IGNORECASE,
)


def _parse_deterministic_quantity(raw: str) -> tuple[Decimal, str] | None:
    match = _QUANTITY_EXPRESSION.fullmatch(raw)
    if match is None:
        return None
    first = _parse_quantity_number(match.group("first"))
    second_text = match.group("second")
    second = _parse_quantity_number(second_text) if second_text is not None else None
    if first is None or (second_text is not None and second is None):
        return None
    amount = first if second is None else (first + second) / Decimal("2")
    if not _is_finite_positive_decimal(amount):
        return None
    return amount, _normalize_unit(match.group("unit"))


def _parse_quantity_number(value: str) -> Decimal | None:
    chinese_values = {
        "半": Decimal("0.5"),
        "一": Decimal("1"),
        "二": Decimal("2"),
        "两": Decimal("2"),
    }
    if value in chinese_values:
        return chinese_values[value]
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return Decimal(numerator) / Decimal(denominator)
        return Decimal(value)
    except (InvalidOperation, ZeroDivisionError):
        return None


def _read_review_csv(path: Path, expected_headers: tuple[str, ...]):
    review_path = Path(path)
    with review_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != expected_headers:
            raise ValueError(
                f"审阅文件列不匹配: {review_path}; expected={expected_headers}"
            )
        yield from reader


def _review_status(row: dict[str, str], path: Path) -> ReviewStatus:
    value = (row.get("review_status") or "").strip()
    if value not in _REVIEW_STATUSES:
        raise ValueError(f"非法 review_status: {value!r}; file={path}")
    return value  # type: ignore[return-value]


def _required(row: dict[str, str], field: str) -> str:
    value = (row.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} 不能为空")
    return value


def _optional_text(row: dict[str, str], field: str) -> str | None:
    value = (row.get(field) or "").strip()
    return value or None


def _optional_decimal(row: dict[str, str], field: str) -> Decimal | None:
    value = (row.get(field) or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} 不是合法数值: {value!r}") from exc


def _required_decimal(row: dict[str, str], field: str) -> Decimal:
    value = _optional_decimal(row, field)
    if value is None:
        raise ValueError(f"{field} 不能为空")
    return value


def _required_int(row: dict[str, str], field: str) -> int:
    value = _required(row, field)
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法整数: {value!r}") from exc
