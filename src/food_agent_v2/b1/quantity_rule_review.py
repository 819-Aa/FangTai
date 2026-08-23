"""Generate auditable, rule-level quantity candidates from model estimates."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from food_agent_v2.b1.quantity_normalizer import MeasureRuleIndex
from food_agent_v2.b1.quantity_review import QuantityReviewCandidate
from food_agent_v2.core.paths import PROJECT_ROOT

QuantityRuleType = Literal["unit_weight", "density", "fuzzy_single_value"]

_EXCEPTION_CODES = (
    "QTY_MAIN_UNRESOLVED",
    "QTY_HIGH_IMPACT_FUZZY",
    "QTY_RULE_DEVIATION_GT_30PCT",
    "QTY_RULE_IQR_OUTLIER",
    "QTY_FORM_USAGE_AMBIGUOUS",
)
_COUNT_UNITS = frozenset({"个", "片", "根", "勺"})
_USAGE_CODES = frozenset(
    {"main", "supporting", "seasoning", "cooking_fat", "retained_liquid"}
)
_FUZZY_TOKEN_CLASSES = frozenset(
    {"as_needed", "small_amount", "several_count", "few_drops"}
)
_UNIT_ALIASES = {
    "g": "克",
    "kg": "千克",
    "ml": "毫升",
    "l": "升",
    "公斤": "千克",
}
_QUANTITY_EXPRESSION = re.compile(
    r"^(?:大约|约)?"
    r"(?P<first>\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?|半|一|二|两)"
    r"(?:(?:-|–|—|~|～|至|到)"
    r"(?P<second>\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?))?"
    r"(?P<unit>千克|公斤|毫升|kg|ml|克|升|l|个|片|根|勺)(?:左右)?$",
    re.IGNORECASE,
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
    coefficient_of_variation: Decimal | None
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
    _validate_positive_values(values, "统计样本")
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
    if not isinstance(p, Decimal) or not p.is_finite() or not Decimal("0") < p <= Decimal("1"):
        raise ValueError("分位数必须在 (0, 1] 内")
    _validate_positive_values(values, "统计样本")
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
        _validate_model_candidate(candidate)
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
    if _is_formal_measure_rule_path(path):
        raise ValueError("数量规则候选不得写入正式计量规则文件")
    candidate_rows = tuple(candidates)
    for candidate in candidate_rows:
        _validate_output_candidate(candidate)
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
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for candidate in candidate_rows:
                writer.writerow(
                    {
                        "rule_type": _safe_csv_text(candidate.rule_type),
                        "ingredient_id": candidate.ingredient_id,
                        "ingredient_name": _safe_csv_text(candidate.ingredient_name),
                        "normalized_form": _safe_csv_text(candidate.normalized_form),
                        "normalized_unit": _safe_csv_text(candidate.normalized_unit),
                        "usage_code": _safe_csv_text(candidate.usage_code),
                        "fuzzy_token_class": _safe_csv_text(
                            candidate.fuzzy_token_class
                        ),
                        "to_grams": _decimal_text(candidate.to_grams),
                        "mass_density_g_per_ml": _decimal_text(
                            candidate.mass_density_g_per_ml
                        ),
                        "sample_count": candidate.sample_count,
                        "mean": candidate.mean,
                        "sample_standard_deviation": candidate.sample_standard_deviation,
                        "coefficient_of_variation": _decimal_text(
                            candidate.coefficient_of_variation
                        ),
                        "q1": candidate.q1,
                        "q3": candidate.q3,
                        "iqr": candidate.iqr,
                        "has_iqr_outlier": str(candidate.has_iqr_outlier).lower(),
                        "is_stable": str(candidate.is_stable).lower(),
                        "exception_codes": _safe_csv_text(
                            "|".join(candidate.exception_codes)
                        ),
                        "review_status": _safe_csv_text(candidate.review_status),
                    }
                )
        if _is_formal_measure_rule_path(path):
            raise ValueError("数量规则候选不得写入正式计量规则文件")
        temporary_path.replace(path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


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
    cv = standard_deviation / mean if len(values) >= 2 else None
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
        is_stable=(
            len(values) >= 5 and cv is not None and cv <= Decimal("0.15")
        ),
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
    parsed = _quantity_amount(candidate.raw_quantity)
    if parsed is None:
        return None
    amount, parsed_unit = parsed
    if unit != parsed_unit:
        return None
    if unit in _COUNT_UNITS:
        return "unit_weight", candidate.candidate_grams / amount
    if unit in {"毫升", "升"}:
        volume_ml = amount * (Decimal("1000") if unit == "升" else Decimal("1"))
        return "density", candidate.candidate_grams / volume_ml
    return None


def _quantity_amount(raw_quantity: str) -> tuple[Decimal, str] | None:
    match = _QUANTITY_EXPRESSION.fullmatch((raw_quantity or "").strip())
    if match is None:
        return None
    first = _parse_quantity_number(match.group("first"))
    second_text = match.group("second")
    second = _parse_quantity_number(second_text) if second_text is not None else None
    if first is None or (second_text is not None and second is None):
        return None
    if first <= 0 or (second is not None and (second <= 0 or first > second)):
        return None
    amount = first if second is None else (first + second) / Decimal("2")
    return amount, _normalized_unit(match.group("unit")) or ""


def _parse_quantity_number(value: str) -> Decimal | None:
    chinese = {
        "半": Decimal("0.5"),
        "一": Decimal("1"),
        "二": Decimal("2"),
        "两": Decimal("2"),
    }
    if value in chinese:
        return chinese[value]
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return Decimal(numerator) / Decimal(denominator)
        return Decimal(value)
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


def _validate_model_candidate(candidate: QuantityReviewCandidate) -> None:
    if type(candidate.ingredient_id) is not int or candidate.ingredient_id < 1:
        raise ValueError("ingredient_id 必须为正整数")
    _require_positive_decimal(candidate.candidate_grams, "candidate_grams")


def _validate_output_candidate(candidate: QuantityRuleCandidate) -> None:
    _validate_rule_text_fields(candidate)
    if candidate.review_status != "pending":
        raise ValueError("数量规则候选必须保持 pending")
    if candidate.rule_type not in {
        "unit_weight",
        "density",
        "fuzzy_single_value",
    }:
        raise ValueError("数量规则候选 rule_type 非法")
    if type(candidate.ingredient_id) is not int or candidate.ingredient_id < 1:
        raise ValueError("数量规则候选 ingredient_id 非法")
    _require_positive_decimal(candidate.mean, "mean")
    _require_positive_decimal(candidate.q1, "q1")
    _require_positive_decimal(candidate.q3, "q3")
    _require_nonnegative_decimal(candidate.sample_standard_deviation, "sample_standard_deviation")
    if type(candidate.sample_count) is not int or candidate.sample_count < 1:
        raise ValueError("数量规则候选 sample_count 非法")
    if candidate.sample_count < 2:
        if candidate.coefficient_of_variation is not None:
            raise ValueError("单样本候选 coefficient_of_variation 必须为空")
        if (
            candidate.sample_standard_deviation != Decimal("0")
            or candidate.q1 != candidate.q3
            or candidate.iqr != Decimal("0")
        ):
            raise ValueError("单样本候选统计量必须为零离散度")
    else:
        _require_nonnegative_decimal(
            candidate.coefficient_of_variation, "coefficient_of_variation"
        )
    _require_nonnegative_decimal(candidate.iqr, "iqr")
    if candidate.q1 > candidate.q3 or candidate.iqr != candidate.q3 - candidate.q1:
        raise ValueError("数量规则候选分位数与 IQR 不一致")
    if type(candidate.has_iqr_outlier) is not bool or type(candidate.is_stable) is not bool:
        raise ValueError("数量规则候选布尔字段非法")
    expected_stability = (
        candidate.sample_count >= 5
        and candidate.coefficient_of_variation is not None
        and candidate.coefficient_of_variation <= Decimal("0.15")
    )
    if candidate.is_stable is not expected_stability:
        raise ValueError("数量规则候选 is_stable 不一致")
    _validate_exception_codes(candidate.exception_codes)
    if candidate.rule_type == "density":
        if candidate.to_grams is not None:
            raise ValueError("density 候选不得包含 to_grams")
        _require_positive_decimal(
            candidate.mass_density_g_per_ml, "mass_density_g_per_ml"
        )
    else:
        if candidate.mass_density_g_per_ml is not None:
            raise ValueError("非 density 候选不得包含 mass_density_g_per_ml")
        _require_positive_decimal(candidate.to_grams, "to_grams")

    if candidate.rule_type == "unit_weight":
        if candidate.normalized_unit not in _COUNT_UNITS:
            raise ValueError("unit_weight 候选单位非法")
        if candidate.usage_code is not None or candidate.fuzzy_token_class is not None:
            raise ValueError("unit_weight 候选不得包含用途或模糊分类")
    elif candidate.rule_type == "density":
        if candidate.normalized_unit not in {"毫升", "升"}:
            raise ValueError("density 候选单位非法")
        if candidate.usage_code is not None or candidate.fuzzy_token_class is not None:
            raise ValueError("density 候选不得包含用途或模糊分类")
    elif candidate.normalized_unit is not None:
        raise ValueError("fuzzy_single_value 候选不得包含单位")
    elif candidate.usage_code is None or candidate.fuzzy_token_class is None:
        raise ValueError("fuzzy_single_value 候选缺少用途或模糊分类")


def _validate_rule_text_fields(candidate: QuantityRuleCandidate) -> None:
    required = (
        candidate.rule_type,
        candidate.ingredient_name,
        candidate.normalized_form,
        candidate.review_status,
    )
    optional = (
        candidate.normalized_unit,
        candidate.usage_code,
        candidate.fuzzy_token_class,
    )
    if any(not isinstance(value, str) for value in required) or any(
        value is not None and not isinstance(value, str) for value in optional
    ):
        raise ValueError("数量规则候选文本字段非法")
    if not candidate.ingredient_name.strip():
        raise ValueError("数量规则候选 ingredient_name 不能为空")
    if candidate.usage_code is not None and candidate.usage_code not in _USAGE_CODES:
        raise ValueError("数量规则候选 usage_code 非法")
    if (
        candidate.fuzzy_token_class is not None
        and candidate.fuzzy_token_class not in _FUZZY_TOKEN_CLASSES
    ):
        raise ValueError("数量规则候选 fuzzy_token_class 非法")
    if candidate.normalized_unit is not None and not candidate.normalized_unit.strip():
        raise ValueError("数量规则候选 normalized_unit 非法")


def _validate_exception_codes(codes: object) -> None:
    if not isinstance(codes, tuple) or any(type(code) is not str for code in codes):
        raise ValueError("exception_codes 非法")
    canonical = tuple(code for code in _EXCEPTION_CODES if code in codes)
    if codes != canonical:
        raise ValueError("exception_codes 必须去重并按固定顺序排列")


def _validate_positive_values(values: tuple[Decimal, ...], name: str) -> None:
    for value in values:
        _require_positive_decimal(value, name)


def _require_positive_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} 必须为有限正 Decimal")
    return value


def _require_nonnegative_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} 必须为有限非负 Decimal")
    return value


def _is_formal_measure_rule_path(path: Path) -> bool:
    formal = PROJECT_ROOT / "data" / "review" / "ingredient_measure_rules.csv"
    if path.name.casefold() == formal.name.casefold():
        return True
    try:
        if str(path.resolve(strict=False)).casefold() == str(
            formal.resolve(strict=False)
        ).casefold():
            return True
        return path.exists() and formal.exists() and path.samefile(formal)
    except OSError:
        return True


def _safe_csv_text(value: str | None) -> str:
    text = value or ""
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text
