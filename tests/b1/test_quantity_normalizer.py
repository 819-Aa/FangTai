from decimal import Decimal

import pytest

from food_agent_v2.b1.consumer_views import NutritionOccurrenceInput
from food_agent_v2.b1.quantity_normalizer import (
    EdibleFractionRule,
    EdibleFractionRuleIndex,
    MeasureRule,
    MeasureRuleIndex,
    QuantityDecision,
    QuantityDecisionIndex,
    load_edible_fraction_rules,
    load_measure_rules,
    load_quantity_decisions,
    normalize_quantity,
)


def _occurrence(raw: str | None, unit: str | None = None) -> NutritionOccurrenceInput:
    return NutritionOccurrenceInput(
        occurrence_id="1-1",
        ingredient_id=10,
        quantity_raw=raw,
        unit_raw=unit,
        quantity_status="explicit" if raw else "unknown",
        ingredient_name="测试食材",
    )


@pytest.mark.parametrize(
    ("raw", "unit", "expected"),
    [
        ("100克", "克", Decimal("100")),
        ("0.5千克", "千克", Decimal("500")),
        ("1斤", "斤", Decimal("500")),
        ("2两", "两", Decimal("100")),
        ("30-40克", "克", Decimal("35")),
        ("约100克", "克", Decimal("100")),
    ],
)
def test_explicit_mass_is_deterministically_normalized(raw, unit, expected) -> None:
    result = normalize_quantity(
        _occurrence(raw, unit), MeasureRuleIndex(()), QuantityDecisionIndex(())
    )

    assert result.standardized_grams == expected
    assert result.requires_review is False


def test_volume_requires_an_approved_density_rule() -> None:
    occurrence = _occurrence("200毫升", "毫升")
    missing = normalize_quantity(occurrence, MeasureRuleIndex(()), QuantityDecisionIndex(()))
    rules = MeasureRuleIndex(
        (
            MeasureRule(
                rule_id="milk-density",
                ingredient_name="测试食材",
                rule_type="density",
                from_unit="毫升",
                to_grams=None,
                mass_density_g_per_ml=Decimal("1.03"),
                review_status="approved",
            ),
        )
    )
    resolved = normalize_quantity(occurrence, rules, QuantityDecisionIndex(()))

    assert missing.standardized_grams is None
    assert resolved.standardized_grams == Decimal("206.00")


def test_measure_unit_aliases_share_the_same_approved_rule() -> None:
    rules = MeasureRuleIndex(
        (
            MeasureRule(
                rule_id="milk-density",
                ingredient_name="测试食材",
                rule_type="density",
                from_unit="毫升",
                to_grams=None,
                mass_density_g_per_ml=Decimal("1.03"),
                review_status="approved",
            ),
        )
    )

    millilitres = normalize_quantity(
        _occurrence("200ml", "ml"), rules, QuantityDecisionIndex(())
    )
    litres = normalize_quantity(
        _occurrence("0.2升", "升"), rules, QuantityDecisionIndex(())
    )

    assert millilitres.standardized_grams == Decimal("206.00")
    assert litres.standardized_grams == Decimal("206.000")


def test_count_uses_an_approved_unit_weight() -> None:
    rules = MeasureRuleIndex(
        (
            MeasureRule(
                rule_id="egg-count",
                ingredient_name="测试食材",
                rule_type="unit_weight",
                from_unit="个",
                to_grams=Decimal("50"),
                mass_density_g_per_ml=None,
                review_status="approved",
            ),
        )
    )

    result = normalize_quantity(
        _occurrence("2个", "个"), rules, QuantityDecisionIndex(())
    )

    assert result.standardized_grams == Decimal("100")


def test_vague_quantity_stays_pending_without_user_decision() -> None:
    result = normalize_quantity(
        _occurrence("少许", None), MeasureRuleIndex(()), QuantityDecisionIndex(())
    )

    assert result.standardized_grams is None
    assert result.requires_review is True
    assert result.review_status == "pending"


def test_approved_or_modified_user_decision_is_authoritative() -> None:
    decisions = QuantityDecisionIndex(
        (
            QuantityDecision(
                occurrence_id="1-1",
                decision_grams=Decimal("3.5"),
                review_status="modified",
            ),
        )
    )

    result = normalize_quantity(_occurrence("少许"), MeasureRuleIndex(()), decisions)

    assert result.standardized_grams == Decimal("3.5")
    assert result.requires_review is False
    assert result.review_status == "modified"


def test_unapproved_measure_rule_is_never_applied() -> None:
    with pytest.raises(ValueError, match="未批准"):
        MeasureRuleIndex(
            (
                MeasureRule(
                    rule_id="bad",
                    ingredient_name="测试食材",
                    rule_type="density",
                    from_unit="毫升",
                    to_grams=None,
                    mass_density_g_per_ml=Decimal("1"),
                    review_status="pending",
                ),
            )
        )


def test_review_csv_loaders_only_activate_allowed_rows(tmp_path) -> None:
    measure_path = tmp_path / "measures.csv"
    measure_path.write_text(
        "rule_id,ingredient_name,rule_type,from_unit,to_grams,mass_density_g_per_ml,review_status\n"
        "milk,牛奶,density,毫升,,1.03,approved\n"
        "salt,盐,unit_weight,勺,5,,pending\n",
        encoding="utf-8",
    )
    decision_path = tmp_path / "decisions.csv"
    decision_path.write_text(
        "occurrence_id,recipe_id,recipe_name,ingredient_name,raw_quantity,step_context,"
        "deterministic_calculation,candidate_basis,candidate_grams,decision_grams,review_status\n"
        "1-1,1,测试菜,盐,少许,加盐,not_available,model,2,3,modified\n"
        "1-2,1,测试菜,葱,少许,加葱,not_available,model,5,,pending\n",
        encoding="utf-8",
    )
    edible_path = tmp_path / "edible.csv"
    edible_path.write_text(
        "ingredient_name,form,edible_fraction,review_status\n"
        "香蕉,带皮,0.66,approved\n"
        "苹果,带皮,0.90,pending\n",
        encoding="utf-8",
    )

    measure_rules = load_measure_rules(measure_path)
    decisions = load_quantity_decisions(decision_path)
    edible_rules = load_edible_fraction_rules(edible_path)

    assert measure_rules.get("牛奶", "毫升") is not None
    assert measure_rules.get("盐", "勺") is None
    assert decisions.get_effective("1-1").decision_grams == Decimal("3")
    assert decisions.get_effective("1-2") is None
    assert edible_rules.get("香蕉", "带皮") == Decimal("0.66")
    assert edible_rules.get("苹果", "带皮") is None


def test_review_csv_loaders_reject_invalid_status_and_effective_blank_decision(
    tmp_path,
) -> None:
    measure_path = tmp_path / "measures.csv"
    measure_path.write_text(
        "rule_id,ingredient_name,rule_type,from_unit,to_grams,mass_density_g_per_ml,review_status\n"
        "bad,牛奶,density,毫升,,1.03,autopassed\n",
        encoding="utf-8",
    )
    decision_path = tmp_path / "decisions.csv"
    decision_path.write_text(
        "occurrence_id,recipe_id,recipe_name,ingredient_name,raw_quantity,step_context,"
        "deterministic_calculation,candidate_basis,candidate_grams,decision_grams,review_status\n"
        "1-1,1,测试菜,盐,少许,加盐,not_available,model,2,,approved\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="review_status"):
        load_measure_rules(measure_path)
    with pytest.raises(ValueError, match="decision_grams"):
        load_quantity_decisions(decision_path)


def test_edible_fraction_rules_require_unique_valid_approved_values(tmp_path) -> None:
    path = tmp_path / "edible.csv"
    path.write_text(
        "ingredient_name,form,edible_fraction,review_status\n"
        "香蕉,带皮,1.20,approved\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="可食比例"):
        load_edible_fraction_rules(path)

    with pytest.raises(ValueError, match="重复可食比例规则"):
        EdibleFractionRuleIndex(
            (
                EdibleFractionRule("香蕉", "带皮", Decimal("0.66"), "approved"),
                EdibleFractionRule("香蕉", "带皮", Decimal("0.70"), "approved"),
            )
        )
