"""食材用量到克的确定性标准化。"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

ReviewStatus = Literal["pending", "approved", "modified", "rejected"]
_REVIEW_STATUSES = {"pending", "approved", "modified", "rejected"}

_MEASURE_HEADERS = (
    "rule_id",
    "ingredient_name",
    "rule_type",
    "from_unit",
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
    rule_id: str
    ingredient_name: str
    rule_type: Literal["density", "unit_weight"]
    from_unit: str
    to_grams: Decimal | None
    mass_density_g_per_ml: Decimal | None
    review_status: ReviewStatus


class MeasureRuleIndex:
    def __init__(self, rules) -> None:
        self._rules: dict[tuple[str, str], MeasureRule] = {}
        for rule in rules:
            if rule.review_status not in _REVIEW_STATUSES:
                raise ValueError(f"计量规则 review_status 非法: {rule.rule_id}")
            if rule.review_status != "approved":
                raise ValueError(f"计量规则未批准: {rule.rule_id}")
            key = (rule.ingredient_name.strip(), _normalize_unit(rule.from_unit))
            if key in self._rules:
                raise ValueError(f"重复计量规则: {key}")
            if rule.rule_type == "density":
                if rule.mass_density_g_per_ml is None or rule.mass_density_g_per_ml <= 0:
                    raise ValueError(f"密度规则无合法密度: {rule.rule_id}")
            elif rule.to_grams is None or rule.to_grams <= 0:
                raise ValueError(f"单位重量规则无合法克重: {rule.rule_id}")
            self._rules[key] = rule

    def get(self, ingredient_name: str, unit: str) -> MeasureRule | None:
        return self._rules.get((ingredient_name.strip(), _normalize_unit(unit)))


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
            if decision.decision_grams <= 0:
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
        if status != "approved":
            continue
        rule_type = row["rule_type"].strip()
        if rule_type not in {"density", "unit_weight"}:
            raise ValueError(f"不支持的 rule_type: {rule_type}")
        rules.append(
            MeasureRule(
                rule_id=_required(row, "rule_id"),
                ingredient_name=_required(row, "ingredient_name"),
                rule_type=rule_type,
                from_unit=_required(row, "from_unit"),
                to_grams=_optional_decimal(row, "to_grams"),
                mass_density_g_per_ml=_optional_decimal(
                    row, "mass_density_g_per_ml"
                ),
                review_status="approved",
            )
        )
    return MeasureRuleIndex(rules)


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
    decision = decisions.get_effective(occurrence.occurrence_id)
    if decision is not None:
        return QuantityNormalizationResult(
            standardized_grams=decision.decision_grams,
            requires_review=False,
            review_status=decision.review_status,
        )

    raw = (occurrence.quantity_raw or "").strip()
    if not raw or any(marker in raw for marker in ("适量", "少许", "若干", "几滴")):
        return _pending()

    values = _numbers(raw)
    if not values:
        return _pending()
    amount = sum(values[:2], Decimal("0")) / Decimal(len(values[:2]))
    unit = _normalize_unit(occurrence.unit_raw or _unit_from_text(raw))

    mass_factors = {
        "克": Decimal("1"),
        "千克": Decimal("1000"),
        "斤": Decimal("500"),
        "两": Decimal("50"),
    }
    if unit in mass_factors:
        return _resolved(amount * mass_factors[unit])

    rule_unit = "毫升" if unit == "升" else unit
    rule = measure_rules.get(occurrence.ingredient_name, rule_unit)
    if rule is None:
        return _pending()
    if rule.rule_type == "density":
        volume_ml = amount * (Decimal("1000") if unit in {"升", "l"} else Decimal("1"))
        return _resolved(volume_ml * rule.mass_density_g_per_ml)
    return _resolved(amount * rule.to_grams)


def _resolved(grams: Decimal) -> QuantityNormalizationResult:
    return QuantityNormalizationResult(grams, False, None)


def _pending() -> QuantityNormalizationResult:
    return QuantityNormalizationResult(None, True, "pending")


def _normalize_unit(unit: str) -> str:
    normalized = (unit or "").strip().lower()
    return {
        "g": "克",
        "kg": "千克",
        "公斤": "千克",
        "ml": "毫升",
        "l": "升",
    }.get(normalized, normalized)


def _unit_from_text(raw: str) -> str:
    known_units = (
        "千克", "公斤", "毫升", "茶匙", "汤匙", "克", "斤", "两", "升", "kg", "ml",
        "g", "l", "个", "只", "片", "勺", "杯", "碗", "颗",
    )
    lowered = raw.lower()
    return next((unit for unit in known_units if unit in lowered), "")


def _numbers(raw: str) -> list[Decimal]:
    cleaned = raw.replace("约", "").replace("大约", "").replace("左右", "")
    tokens = re.findall(r"\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?", cleaned)
    values: list[Decimal] = []
    for token in tokens:
        try:
            if "/" in token:
                numerator, denominator = token.split("/", 1)
                value = Decimal(numerator) / Decimal(denominator)
            else:
                value = Decimal(token)
        except (InvalidOperation, ZeroDivisionError):
            return []
        values.append(value)
    return values


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
