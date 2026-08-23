"""Generate auditable, rule-level quantity candidates from model estimates."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from food_agent_v2.b1.quantity_normalizer import MeasureRuleIndex
from food_agent_v2.b1.quantity_review import QuantityReviewCandidate

QuantityRuleType = Literal["unit_weight", "density", "fuzzy_single_value"]

_EXCEPTION_CODES = (
    "QTY_MAIN_UNRESOLVED",
    "QTY_HIGH_IMPACT_FUZZY",
    "QTY_RULE_DEVIATION_GT_30PCT",
    "QTY_RULE_IQR_OUTLIER",
    "QTY_FORM_USAGE_AMBIGUOUS",
)
_COUNT_UNITS = frozenset({"个", "片", "根", "勺"})
_UNIT_ALIASES = {
    "g": "克",
    "kg": "千克",
    "ml": "毫升",
    "l": "升",
    "公斤": "千克",
}
_AMOUNT_PATTERN = re.compile(
    r"^(?:大约|约)?(?P<amount>\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?|半|一|二|两)"
)


@dataclass(frozen=True)
class QuantityRuleCandidate:
    """A pending proposal; it is never an effective measure rule."""

    rule_type: QuantityRuleType
    ingredient_id: int
    ingredient_name: str
    normalized_form: str
    normalized_unit: str | None
    usage_code: str | None
    fuzzy_token_class: str | None
    to_grams: Decimal | None
    mass_density_g_per_ml: Decimal | None
    sample_count: int
    mean: Decimal
    sample_standard_deviation: Decimal
    coefficient_of_variation: Decimal
    q1: Decimal
    q3: Decimal
    iqr: Decimal
    has_iqr_outlier: bool
    is_stable: bool
    exception_codes: tuple[str, ...]
    review_status: Literal["pending"] = "pending"


def coefficient_of_variation(values: tuple[Decimal, ...]) -> Decimal:
    """Return sample-CV, retaining Decimal precision throughout."""
    if len(values) < 2:
        raise ValueError("至少需要两个样本计算变异系数")
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    if mean <= 0:
        raise ValueError("样本均值必须大于零")
    variance = sum((value - mean) ** 2 for value in values) / Decimal(
        len(values) - 1
    )
    return variance.sqrt() / mean


def nearest_rank(values: tuple[Decimal, ...], p: Decimal) -> Decimal:
    """Return a nearest-rank percentile without converting to float."""
    if not values:
        raise ValueError("空样本不能计算分位数")
    if not Decimal("0") < p <= Decimal("1"):
        raise ValueError("分位数必须在 (0, 1] 内")
    ordered = sorted(values)
    rank = (p * Decimal(len(ordered))).to_integral_value(rounding=ROUND_CEILING)
    return ordered[max(1, int(rank)) - 1]


def generate_quantity_rule_candidates(
    candidates: tuple[QuantityReviewCandidate, ...] | list[QuantityReviewCandidate],
    measure_rules: MeasureRuleIndex,
) -> tuple[QuantityRuleCandidate, ...]:
    """Group exact V2 keys and emit review-only measure-rule proposals.

    Input estimates remain evidence.  This function deliberately does not write
    a measure-rule file, and every result stays ``pending`` for human review.
    """
    observations: dict[tuple, list[Decimal]] = defaultdict(list)
    group_names: dict[tuple, str] = {}
    forms_by_ingredient: dict[int, set[tuple[str, str | None, str | None]]] = (
        defaultdict(set)
    )
    for candidate in candidates:
        observation = _rule_observation(candidate)
        if observation is None:
            continue
        rule_type, value = observation
        key = (
            rule_type,
            candidate.ingredient_id,
            (candidate.normalized_form or "").strip(),
            _normalized_unit(candidate.normalized_unit),
            candidate.usage_code,
            candidate.fuzzy_token_class,
        )
        observations[key].append(value)
        group_names.setdefault(key, candidate.ingredient_name)
        forms_by_ingredient[candidate.ingredient_id].add(
            (
                (candidate.normalized_form or "").strip(),
                candidate.usage_code,
                candidate.fuzzy_token_class,
            )
        )

    result: list[QuantityRuleCandidate] = []
    for key in sorted(observations, key=_sort_group_key):
        rule_type, ingredient_id, form, unit, usage, fuzzy = key
        values = tuple(observations[key])
        result.append(
            _build_rule_candidate(
                rule_type=rule_type,
                ingredient_id=ingredient_id,
                ingredient_name=group_names[key],
                normalized_form=form,
                normalized_unit=unit,
                usage_code=usage,
                fuzzy_token_class=fuzzy,
                values=values,
                measure_rules=measure_rules,
                form_usage_ambiguous=len(forms_by_ingredient[ingredient_id]) > 1,
            )
        )
    return tuple(result)


def write_quantity_rule_candidates(
    candidates: tuple[QuantityRuleCandidate, ...] | list[QuantityRuleCandidate],
    output_path: Path,
) -> None:
    """Write review data only, refusing any status that could become effective."""
    path = Path(output_path)
    if path.name == "ingredient_measure_rules.csv":
        raise ValueError("数量规则候选不得写入正式计量规则文件")
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        "rule_type",
        "ingredient_id",
        "ingredient_name",
        "normalized_form",
        "normalized_unit",
        "usage_code",
        "fuzzy_token_class",
        "to_grams",
        "mass_density_g_per_ml",
        "sample_count",
        "mean",
        "sample_standard_deviation",
        "coefficient_of_variation",
        "q1",
        "q3",
        "iqr",
        "has_iqr_outlier",
        "is_stable",
        "exception_codes",
        "review_status",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            if candidate.review_status != "pending":
                raise ValueError("数量规则候选必须保持 pending")
            writer.writerow(
                {
                    "rule_type": candidate.rule_type,
                    "ingredient_id": candidate.ingredient_id,
                    "ingredient_name": candidate.ingredient_name,
                    "normalized_form": candidate.normalized_form,
                    "normalized_unit": candidate.normalized_unit or "",
                    "usage_code": candidate.usage_code or "",
                    "fuzzy_token_class": candidate.fuzzy_token_class or "",
                    "to_grams": _decimal_text(candidate.to_grams),
                    "mass_density_g_per_ml": _decimal_text(
                        candidate.mass_density_g_per_ml
                    ),
                    "sample_count": candidate.sample_count,
                    "mean": candidate.mean,
                    "sample_standard_deviation": candidate.sample_standard_deviation,
                    "coefficient_of_variation": candidate.coefficient_of_variation,
                    "q1": candidate.q1,
                    "q3": candidate.q3,
                    "iqr": candidate.iqr,
                    "has_iqr_outlier": str(candidate.has_iqr_outlier).lower(),
                    "is_stable": str(candidate.is_stable).lower(),
                    "exception_codes": "|".join(candidate.exception_codes),
                    "review_status": "pending",
                }
            )


def _build_rule_candidate(
    *,
    rule_type: QuantityRuleType,
    ingredient_id: int,
    ingredient_name: str,
    normalized_form: str,
    normalized_unit: str | None,
    usage_code: str | None,
    fuzzy_token_class: str | None,
    values: tuple[Decimal, ...],
    measure_rules: MeasureRuleIndex,
    form_usage_ambiguous: bool,
) -> QuantityRuleCandidate:
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    standard_deviation = _sample_standard_deviation(values, mean)
    cv = (
        standard_deviation / mean if len(values) >= 2 and mean > 0 else Decimal("Infinity")
    )
    q1 = nearest_rank(values, Decimal("0.25"))
    q3 = nearest_rank(values, Decimal("0.75"))
    iqr = q3 - q1
    lower = q1 - Decimal("1.5") * iqr
    upper = q3 + Decimal("1.5") * iqr
    has_iqr_outlier = any(value < lower or value > upper for value in values)
    existing = _existing_rule_value(
        measure_rules,
        rule_type,
        ingredient_id,
        normalized_form,
        normalized_unit,
        usage_code,
        fuzzy_token_class,
    )
    codes: set[str] = set()
    if usage_code == "main":
        codes.add("QTY_MAIN_UNRESOLVED")
    if fuzzy_token_class is not None and any(
        marker in ingredient_name for marker in ("盐", "油", "糖")
    ):
        codes.add("QTY_HIGH_IMPACT_FUZZY")
    if existing is not None and abs(mean - existing) / existing > Decimal("0.30"):
        codes.add("QTY_RULE_DEVIATION_GT_30PCT")
    if has_iqr_outlier:
        codes.add("QTY_RULE_IQR_OUTLIER")
    if form_usage_ambiguous:
        codes.add("QTY_FORM_USAGE_AMBIGUOUS")
    return QuantityRuleCandidate(
        rule_type=rule_type,
        ingredient_id=ingredient_id,
        ingredient_name=ingredient_name,
        normalized_form=normalized_form,
        normalized_unit=normalized_unit,
        usage_code=usage_code,
        fuzzy_token_class=fuzzy_token_class,
        to_grams=mean if rule_type != "density" else None,
        mass_density_g_per_ml=mean if rule_type == "density" else None,
        sample_count=len(values),
        mean=mean,
        sample_standard_deviation=standard_deviation,
        coefficient_of_variation=cv,
        q1=q1,
        q3=q3,
        iqr=iqr,
        has_iqr_outlier=has_iqr_outlier,
        is_stable=len(values) >= 5 and cv <= Decimal("0.15"),
        exception_codes=tuple(code for code in _EXCEPTION_CODES if code in codes),
    )


def _sample_standard_deviation(values: tuple[Decimal, ...], mean: Decimal) -> Decimal:
    if len(values) < 2:
        return Decimal("0")
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values) - 1)
    return variance.sqrt()


def _rule_observation(
    candidate: QuantityReviewCandidate,
) -> tuple[QuantityRuleType, Decimal] | None:
    unit = _normalized_unit(candidate.normalized_unit)
    if candidate.fuzzy_token_class is not None and candidate.usage_code is not None:
        return "fuzzy_single_value", candidate.candidate_grams
    amount = _quantity_amount(candidate.raw_quantity)
    if amount is None:
        return None
    if unit in _COUNT_UNITS:
        return "unit_weight", candidate.candidate_grams / amount
    if unit in {"毫升", "升"}:
        volume_ml = amount * (Decimal("1000") if unit == "升" else Decimal("1"))
        return "density", candidate.candidate_grams / volume_ml
    return None


def _quantity_amount(raw_quantity: str) -> Decimal | None:
    match = _AMOUNT_PATTERN.match((raw_quantity or "").strip())
    if match is None:
        return None
    text = match.group("amount")
    chinese = {"半": Decimal("0.5"), "一": Decimal("1"), "二": Decimal("2"), "两": Decimal("2")}
    if text in chinese:
        return chinese[text]
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return Decimal(numerator) / Decimal(denominator)
        return Decimal(text)
    except (InvalidOperation, ZeroDivisionError):
        return None


def _existing_rule_value(
    measure_rules: MeasureRuleIndex,
    rule_type: QuantityRuleType,
    ingredient_id: int,
    form: str,
    unit: str | None,
    usage: str | None,
    fuzzy: str | None,
) -> Decimal | None:
    if rule_type == "unit_weight" and unit is not None:
        rule = measure_rules.get_unit_weight(ingredient_id, form, unit)
        return rule.to_grams if rule is not None else None
    if rule_type == "density":
        rule = measure_rules.get_density(ingredient_id, form)
        return rule.mass_density_g_per_ml if rule is not None else None
    if usage is not None and fuzzy is not None:
        rule = measure_rules.get_fuzzy_single_value(ingredient_id, form, usage, fuzzy)
        return rule.to_grams if rule is not None else None
    return None


def _normalized_unit(unit: str | None) -> str | None:
    value = (unit or "").strip().lower()
    return _UNIT_ALIASES.get(value, value) or None


def _sort_group_key(key: tuple) -> tuple[str, ...]:
    return tuple("" if value is None else str(value) for value in key)


def _decimal_text(value: Decimal | None) -> str:
    return "" if value is None else str(value)
