from decimal import Decimal

import pytest

from food_agent_v2.b1.consumer_views import NutritionOccurrenceInput
from food_agent_v2.b1.quantity_normalizer import (
    MeasureRule,
    MeasureRuleIndex,
    QuantityDecision,
    QuantityDecisionIndex,
    load_measure_rules,
    normalize_quantity,
)


def _occurrence(
    raw: str | None = None,
    unit: str | None = None,
    *,
    occurrence_id: str = "1-1",
    ingredient_id: int = 10,
    form: str = "raw",
    usage: str | None = "seasoning",
    token: str | None = None,
    retained: bool | None = True,
    requires_review: bool = False,
) -> NutritionOccurrenceInput:
    return NutritionOccurrenceInput(
        occurrence_id=occurrence_id,
        ingredient_id=ingredient_id,
        quantity_raw=raw,
        unit_raw=unit,
        quantity_status="explicit" if raw else "unknown",
        ingredient_name="测试食材",
        normalized_form=form,
        usage_code=usage,  # type: ignore[arg-type]
        fuzzy_token_class=token,  # type: ignore[arg-type]
        retained_in_dish=retained,
        requires_review=requires_review,
    )


def _rule(
    *,
    rule_id: str = "rule-1",
    rule_type: str,
    ingredient_id: int = 10,
    form: str = "raw",
    unit: str | None = None,
    usage: str | None = None,
    token: str | None = None,
    grams: str | None = None,
    density: str | None = None,
    status: str = "approved",
) -> MeasureRule:
    return MeasureRule(
        schema_version="2.0.0",
        rule_id=rule_id,
        rule_type=rule_type,  # type: ignore[arg-type]
        ingredient_id=ingredient_id,
        ingredient_name="测试食材",
        normalized_form=form,
        normalized_unit=unit,
        usage_code=usage,  # type: ignore[arg-type]
        fuzzy_token_class=token,  # type: ignore[arg-type]
        to_grams=Decimal(grams) if grams is not None else None,
        mass_density_g_per_ml=Decimal(density) if density is not None else None,
        review_status=status,  # type: ignore[arg-type]
    )


def _fuzzy_rule(**kwargs) -> MeasureRule:
    return _rule(rule_type="fuzzy_single_value", **kwargs)


@pytest.mark.parametrize(
    ("raw", "unit", "expected"),
    [
        ("100克", "克", Decimal("100")),
        ("0.5千克", "千克", Decimal("500")),
        ("1斤", "斤", Decimal("500")),
        ("2两", "两", Decimal("100")),
        ("半斤", "斤", Decimal("250")),
        ("一两", "两", Decimal("50")),
    ],
)
def test_direct_mass_conversions_are_deterministic(raw, unit, expected) -> None:
    result = normalize_quantity(
        _occurrence(raw, unit), MeasureRuleIndex(()), QuantityDecisionIndex(())
    )

    assert result.standardized_grams == expected
    assert result.requires_review is False


def test_occurrence_decision_has_highest_priority() -> None:
    rules = MeasureRuleIndex(
        (_rule(rule_type="unit_weight", unit="个", grams="50"),)
    )
    decisions = QuantityDecisionIndex(
        (
            QuantityDecision(
                occurrence_id="1-1",
                decision_grams=Decimal("3.5"),
                review_status="modified",
            ),
        )
    )

    result = normalize_quantity(_occurrence("2个", "个"), rules, decisions)

    assert result.standardized_grams == Decimal("3.5")
    assert result.review_status == "modified"


def test_half_and_chinese_number_with_count_unit_use_unit_weight() -> None:
    rules = MeasureRuleIndex(
        (_rule(rule_type="unit_weight", unit="个", grams="50"),)
    )

    half = normalize_quantity(_occurrence("半个", "个"), rules, QuantityDecisionIndex(()))
    two = normalize_quantity(_occurrence("两个", "个"), rules, QuantityDecisionIndex(()))

    assert half.standardized_grams == Decimal("25")
    assert two.standardized_grams == Decimal("100")


def test_density_only_applies_to_ml_or_l() -> None:
    rules = MeasureRuleIndex(
        (_rule(rule_type="density", unit="毫升", density="1.03"),)
    )

    millilitres = normalize_quantity(
        _occurrence("200ml", "ml"), rules, QuantityDecisionIndex(())
    )
    litres = normalize_quantity(
        _occurrence("0.2升", "升"), rules, QuantityDecisionIndex(())
    )

    assert millilitres.standardized_grams == Decimal("206.00")
    assert litres.standardized_grams == Decimal("206.000")
    with pytest.raises(ValueError, match="密度规则"):
        MeasureRuleIndex((_rule(rule_type="density", unit="个", density="1"),))


def test_unit_weight_only_applies_to_count_units() -> None:
    with pytest.raises(ValueError, match="单位重量规则"):
        MeasureRuleIndex((_rule(rule_type="unit_weight", unit="毫升", grams="5"),))


def test_fuzzy_rule_requires_exact_form_usage_and_token() -> None:
    rules = MeasureRuleIndex(
        (
            _fuzzy_rule(
                ingredient_id=10,
                form="净料",
                usage="seasoning",
                token="small_amount",
                grams="2.5",
            ),
        )
    )
    assert normalize_quantity(
        _occurrence(form="净料", usage="seasoning", token="small_amount"),
        rules,
        QuantityDecisionIndex(()),
    ).standardized_grams == Decimal("2.5")
    assert normalize_quantity(
        _occurrence(form="净料", usage="main", token="small_amount"),
        rules,
        QuantityDecisionIndex(()),
    ).requires_review is True
    assert normalize_quantity(
        _occurrence(form="净料", usage="seasoning", token="few_drops"),
        rules,
        QuantityDecisionIndex(()),
    ).requires_review is True
    assert normalize_quantity(
        _occurrence(form="生料", usage="seasoning", token="small_amount"),
        rules,
        QuantityDecisionIndex(()),
    ).requires_review is True
    assert normalize_quantity(
        _occurrence(
            ingredient_id=11,
            form="净料",
            usage="seasoning",
            token="small_amount",
        ),
        rules,
        QuantityDecisionIndex(()),
    ).requires_review is True


@pytest.mark.parametrize("raw", ["数个", "几根"])
def test_indefinite_counts_remain_pending(raw) -> None:
    rules = MeasureRuleIndex(
        (_rule(rule_type="unit_weight", unit="个", grams="50"),)
    )

    result = normalize_quantity(_occurrence(raw, "个"), rules, QuantityDecisionIndex(()))

    assert result.standardized_grams is None
    assert result.requires_review is True


def test_measure_rule_csv_requires_v2_header_and_only_activates_approved_rows(
    tmp_path,
) -> None:
    path = tmp_path / "measures.csv"
    path.write_text(
        "schema_version,rule_id,rule_type,ingredient_id,ingredient_name,normalized_form,"
        "normalized_unit,usage_code,fuzzy_token_class,to_grams,mass_density_g_per_ml,review_status\n"
        "2.0.0,egg,unit_weight,10,鸡蛋,raw,个,,,50,,approved\n"
        "2.0.0,salt,fuzzy_single_value,20,盐,raw,,seasoning,small_amount,2,,pending\n"
        "2.0.0,oil,density,30,油,raw,毫升,,,,0.9,rejected\n",
        encoding="utf-8",
    )
    old_header = tmp_path / "old.csv"
    old_header.write_text(
        "rule_id,ingredient_name,rule_type,from_unit,to_grams,mass_density_g_per_ml,review_status\n",
        encoding="utf-8",
    )

    rules = load_measure_rules(path)

    assert rules.get_unit_weight(10, "raw", "个") is not None
    assert rules.get_fuzzy_single_value(20, "raw", "seasoning", "small_amount") is None
    with pytest.raises(ValueError, match="列不匹配"):
        load_measure_rules(old_header)


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_inactive_measure_rules_are_rejected_by_the_index(status) -> None:
    with pytest.raises(ValueError, match="未批准"):
        MeasureRuleIndex(
            (_rule(rule_type="unit_weight", unit="个", grams="50", status=status),)
        )


def test_measure_rule_index_rejects_duplicate_v2_keys() -> None:
    rule = _rule(rule_id="egg-a", rule_type="unit_weight", unit="个", grams="50")
    duplicate = _rule(rule_id="egg-b", rule_type="unit_weight", unit="个", grams="55")

    with pytest.raises(ValueError, match="重复计量规则"):
        MeasureRuleIndex((rule, duplicate))


@pytest.mark.parametrize(
    ("usage", "retained", "requires_review"),
    [
        ("seasoning", True, True),
        (None, True, False),
        ("seasoning", None, False),
    ],
)
def test_unresolved_task1_occurrence_cannot_be_overridden_by_quantity_decision(
    usage, retained, requires_review
) -> None:
    decisions = QuantityDecisionIndex(
        (
            QuantityDecision(
                occurrence_id="1-1",
                decision_grams=Decimal("3.5"),
                review_status="approved",
            ),
        )
    )

    result = normalize_quantity(
        _occurrence(
            "100克",
            "克",
            usage=usage,
            retained=retained,
            requires_review=requires_review,
        ),
        MeasureRuleIndex(()),
        decisions,
    )

    assert result.standardized_grams is None
    assert result.requires_review is True


@pytest.mark.parametrize(
    "raw,unit",
    [("两根", "根"), ("两根", None), ("两片", None), ("两个", None), ("两勺", None)],
)
def test_chinese_two_uses_the_explicit_count_unit_not_mass_liang(raw, unit) -> None:
    rules = MeasureRuleIndex(
        (
            _rule(rule_id="root", rule_type="unit_weight", unit="根", grams="10"),
            _rule(rule_id="slice", rule_type="unit_weight", unit="片", grams="10"),
            _rule(rule_id="piece", rule_type="unit_weight", unit="个", grams="10"),
            _rule(rule_id="spoon", rule_type="unit_weight", unit="勺", grams="10"),
        )
    )

    result = normalize_quantity(_occurrence(raw, unit), rules, QuantityDecisionIndex(()))

    assert result.standardized_grams == Decimal("20")


@pytest.mark.parametrize("raw,unit", [("-100克", "克"), ("100克（2人份）", "克"), ("100克2", "克")])
def test_quantity_parser_rejects_negative_and_extra_numbers(raw, unit) -> None:
    result = normalize_quantity(
        _occurrence(raw, unit), MeasureRuleIndex(()), QuantityDecisionIndex(())
    )

    assert result.standardized_grams is None
    assert result.requires_review is True


def test_quantity_parser_accepts_only_one_anchored_range_or_fraction() -> None:
    rules = MeasureRuleIndex(
        (_rule(rule_type="unit_weight", unit="个", grams="50"),)
    )

    ranged = normalize_quantity(
        _occurrence("30-40克", "克"), MeasureRuleIndex(()), QuantityDecisionIndex(())
    )
    fraction = normalize_quantity(
        _occurrence("1/2个", "个"), rules, QuantityDecisionIndex(())
    )

    assert ranged.standardized_grams == Decimal("35")
    assert fraction.standardized_grams == Decimal("25")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1"])
def test_rules_and_quantity_decisions_require_finite_positive_decimals(value) -> None:
    with pytest.raises(ValueError, match="克重"):
        MeasureRuleIndex(
            (_rule(rule_type="unit_weight", unit="个", grams=value),)
        )
    with pytest.raises(ValueError, match="必须大于零"):
        QuantityDecisionIndex(
            (
                QuantityDecision(
                    occurrence_id="1-1",
                    decision_grams=Decimal(value),
                    review_status="approved",
                ),
            )
        )


@pytest.mark.parametrize("ingredient_id", [True, 10.0, 0, -1])
def test_measure_rule_requires_a_true_positive_integer_ingredient_id(ingredient_id) -> None:
    with pytest.raises(ValueError, match="ingredient_id"):
        MeasureRuleIndex(
            (
                _rule(
                    rule_type="unit_weight",
                    ingredient_id=ingredient_id,
                    unit="个",
                    grams="50",
                ),
            )
        )


def test_measure_rule_rejects_surrounding_rule_id_whitespace() -> None:
    with pytest.raises(ValueError, match="rule_id"):
        MeasureRuleIndex(
            (_rule(rule_id=" rule-1 ", rule_type="unit_weight", unit="个", grams="50"),)
        )


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("2.0.0,bad,unit_weight,10.5,蛋,raw,个,,,50,,pending", "ingredient_id"),
        ("2.0.0,egg,unit_weight,10,蛋,raw,个,,,50,,pending", "rule_id"),
        ("2.0.0,bad,unit_weight,10,蛋,raw,个,,,NaN,,rejected", "克重"),
    ],
)
def test_inactive_measure_rows_are_fully_validated_before_being_ignored(
    tmp_path, row, message
) -> None:
    path = tmp_path / "measures.csv"
    path.write_text(
        "schema_version,rule_id,rule_type,ingredient_id,ingredient_name,normalized_form,"
        "normalized_unit,usage_code,fuzzy_token_class,to_grams,mass_density_g_per_ml,review_status\n"
        "2.0.0,egg,unit_weight,10,蛋,raw,个,,,50,,approved\n"
        f"{row}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_measure_rules(path)


def test_inactive_audit_rules_are_never_queryable() -> None:
    audit_index = MeasureRuleIndex(
        (
            _rule(
                rule_type="unit_weight",
                unit="个",
                grams="50",
                status="pending",
            ),
        ),
        _allow_inactive=True,
    )

    assert audit_index.get_unit_weight(10, "raw", "个") is None


@pytest.mark.parametrize("raw", ["0-10克", "40-30克"])
def test_quantity_ranges_require_positive_ordered_endpoints(raw) -> None:
    result = normalize_quantity(
        _occurrence(raw, "克"), MeasureRuleIndex(()), QuantityDecisionIndex(())
    )

    assert result.standardized_grams is None
    assert result.requires_review is True
