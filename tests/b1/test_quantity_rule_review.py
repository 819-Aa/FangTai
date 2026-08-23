from decimal import Decimal

import pytest

from food_agent_v2.b1.quantity_normalizer import MeasureRule, MeasureRuleIndex
from food_agent_v2.b1.quantity_review import QuantityReviewCandidate
from food_agent_v2.b1.quantity_rule_review import (
    generate_quantity_rule_candidates,
    write_quantity_rule_candidates,
)


def _candidate(
    value: str,
    *,
    ingredient_id: int = 10,
    ingredient_name: str = "葱",
    form: str = "切段",
    unit: str | None = None,
    usage: str = "supporting",
    fuzzy: str | None = "small_amount",
) -> QuantityReviewCandidate:
    return QuantityReviewCandidate(
        occurrence_id=f"r-{ingredient_id}-{form}-{unit}-{usage}-{fuzzy}-{value}",
        recipe_id=1,
        recipe_name="测试菜",
        ingredient_id=ingredient_id,
        ingredient_name=ingredient_name,
        normalized_form=form,
        normalized_unit=unit,
        usage_code=usage,
        fuzzy_token_class=fuzzy,
        raw_quantity="少许" if fuzzy else f"1{unit or ''}",
        step_context="",
        deterministic_calculation="not_available",
        candidate_basis="whole_recipe_context_model",
        candidate_grams=Decimal(value),
    )


def _fuzzy_rule(grams: str = "10") -> MeasureRule:
    return MeasureRule(
        schema_version="2.0.0",
        rule_id="fuzzy-onion",
        rule_type="fuzzy_single_value",
        ingredient_id=10,
        ingredient_name="葱",
        normalized_form="切段",
        normalized_unit=None,
        usage_code="supporting",
        fuzzy_token_class="small_amount",
        to_grams=Decimal(grams),
        mass_density_g_per_ml=None,
        review_status="approved",
    )


def test_rule_candidate_requires_five_samples_and_cv_at_most_point_15() -> None:
    rows = tuple(_candidate(value) for value in ("9", "10", "10", "10", "11"))

    result = generate_quantity_rule_candidates(rows, MeasureRuleIndex(()))

    assert result[0].sample_count == 5
    assert result[0].sample_standard_deviation > Decimal("0")
    assert result[0].coefficient_of_variation <= Decimal("0.15")
    assert result[0].is_stable is True
    assert result[0].review_status == "pending"


def test_rule_candidate_uses_nearest_rank_iqr_and_strict_30_percent_boundary() -> None:
    rows = tuple(_candidate(value) for value in ("11", "12", "13", "14", "15"))

    at_boundary = generate_quantity_rule_candidates(rows, MeasureRuleIndex((_fuzzy_rule(),)))[0]
    above_boundary = generate_quantity_rule_candidates(
        tuple(_candidate(value) for value in ("13.01",) * 5),
        MeasureRuleIndex((_fuzzy_rule(),)),
    )[0]

    assert (at_boundary.q1, at_boundary.q3, at_boundary.iqr) == (
        Decimal("12"),
        Decimal("14"),
        Decimal("2"),
    )
    assert "QTY_RULE_DEVIATION_GT_30PCT" not in at_boundary.exception_codes
    assert "QTY_RULE_DEVIATION_GT_30PCT" in above_boundary.exception_codes


def test_rule_candidate_flags_iqr_outliers_and_keeps_them_pending() -> None:
    result = generate_quantity_rule_candidates(
        tuple(_candidate(value) for value in ("9", "10", "10", "10", "30")),
        MeasureRuleIndex(()),
    )

    assert result[0].has_iqr_outlier is True
    assert result[0].exception_codes == ("QTY_RULE_IQR_OUTLIER",)
    assert result[0].review_status == "pending"


def test_rule_candidate_strictly_isolates_form_usage_and_fuzzy_groups() -> None:
    rows = (
        tuple(_candidate("2", form="切段") for _ in range(5))
        + tuple(_candidate("7", form="整根", usage="main") for _ in range(5))
    )

    result = generate_quantity_rule_candidates(rows, MeasureRuleIndex(()))

    assert len(result) == 2
    assert {item.normalized_form for item in result} == {"切段", "整根"}
    assert all("QTY_FORM_USAGE_AMBIGUOUS" in item.exception_codes for item in result)
    assert all(item.review_status == "pending" for item in result)


def test_high_impact_main_and_salt_oil_sugar_fuzzy_candidates_are_never_approved() -> None:
    rows = (
        tuple(_candidate("20", ingredient_name="鸡肉", usage="main") for _ in range(5))
        + tuple(_candidate("2", ingredient_id=11, ingredient_name="盐") for _ in range(5))
    )

    result = generate_quantity_rule_candidates(rows, MeasureRuleIndex(()))
    by_name = {item.ingredient_name: item for item in result}

    assert "QTY_MAIN_UNRESOLVED" in by_name["鸡肉"].exception_codes
    assert "QTY_HIGH_IMPACT_FUZZY" in by_name["盐"].exception_codes
    assert {item.review_status for item in result} == {"pending"}


def test_rule_candidate_writer_refuses_the_formal_measure_rule_filename(tmp_path) -> None:
    candidates = generate_quantity_rule_candidates(
        tuple(_candidate("2") for _ in range(5)), MeasureRuleIndex(())
    )

    with pytest.raises(ValueError, match="正式计量规则文件"):
        write_quantity_rule_candidates(candidates, tmp_path / "ingredient_measure_rules.csv")
