from decimal import Decimal
from pathlib import Path

import pytest

from food_agent_v2.b1.consumer_views import NutritionOccurrenceInput
from food_agent_v2.b1.edible_fraction_review import (
    EdibleFractionDecision,
    EdibleFractionDecisionIndex,
    EdibleFractionRule,
    EdibleFractionRuleIndex,
    load_edible_fraction_decisions,
    load_edible_fraction_rules,
    resolve_edible_fraction,
)


def _occurrence(
    *, id: str = "1-1", ingredient_id: int = 7, form: str = "带皮"
) -> NutritionOccurrenceInput:
    return NutritionOccurrenceInput(
        occurrence_id=id,
        ingredient_id=ingredient_id,
        quantity_raw="100克",
        unit_raw="克",
        quantity_status="explicit",
        ingredient_name="苹果",
        normalized_form=form,
    )


def _rule(ingredient_id: int, normalized_form: str, edible_fraction: str) -> EdibleFractionRule:
    return EdibleFractionRule(
        ingredient_id=ingredient_id,
        ingredient_name="苹果",
        normalized_form=normalized_form,
        edible_fraction=Decimal(edible_fraction),
        review_status="approved",
    )


def _decision(
    occurrence_id: str, edible_fraction: str, review_status: str
) -> EdibleFractionDecision:
    return EdibleFractionDecision(
        occurrence_id=occurrence_id,
        recipe_id=1,
        ingredient_id=7,
        ingredient_name="苹果",
        normalized_form="带皮",
        decision_form="带皮",
        edible_fraction=Decimal(edible_fraction),
        review_status=review_status,
    )


def test_occurrence_decision_overrides_generic_fraction() -> None:
    resolution = resolve_edible_fraction(
        _occurrence(id="1-1", ingredient_id=7, form="带皮"),
        EdibleFractionRuleIndex((_rule(7, "带皮", "0.80"),)),
        EdibleFractionDecisionIndex((_decision("1-1", "0.65", "modified"),)),
    )

    assert resolution.edible_fraction == Decimal("0.65")
    assert resolution.requires_review is False


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-0.1", "1.01"])
def test_fractions_must_be_finite_and_in_the_edible_interval(value: str) -> None:
    with pytest.raises(ValueError, match="可食比例"):
        EdibleFractionRuleIndex((_rule(7, "带皮", value),))


def test_rule_key_uses_ingredient_id_and_normalized_form() -> None:
    with pytest.raises(ValueError, match="重复可食比例规则"):
        EdibleFractionRuleIndex(
            (
                _rule(7, "带皮", "0.8"),
                EdibleFractionRule(7, "另一名称", "带皮", Decimal("0.7"), "approved"),
            )
        )


def test_pending_rules_and_decisions_do_not_become_effective() -> None:
    resolution = resolve_edible_fraction(
        _occurrence(),
        EdibleFractionRuleIndex(
            (EdibleFractionRule(7, "苹果", "带皮", Decimal("0.8"), "pending"),)
        ),
        EdibleFractionDecisionIndex((_decision("1-1", "0.65", "pending"),)),
    )

    assert resolution.edible_fraction is None
    assert resolution.requires_review is True


def test_active_decision_metadata_drift_fails_closed_without_generic_fallback() -> None:
    decision = EdibleFractionDecision(
        occurrence_id="1-1",
        recipe_id=9,
        ingredient_id=7,
        ingredient_name="苹果",
        normalized_form="带皮",
        decision_form="带皮",
        edible_fraction=Decimal("0.65"),
        review_status="approved",
    )

    resolution = resolve_edible_fraction(
        _occurrence(),
        EdibleFractionRuleIndex((_rule(7, "带皮", "0.8"),)),
        EdibleFractionDecisionIndex((decision,)),
    )

    assert resolution.edible_fraction is None
    assert resolution.requires_review is True


def test_blank_form_rule_cannot_coexist_with_a_specific_form() -> None:
    with pytest.raises(ValueError, match="空白形态"):
        EdibleFractionRuleIndex((_rule(7, "", "1"), _rule(7, "带皮", "0.8")))


def test_loaders_require_the_formal_headers_and_reject_cooking_yield_fields(
    tmp_path: Path,
) -> None:
    rules = tmp_path / "rules.csv"
    rules.write_text(
        "ingredient_id,ingredient_name,normalized_form,edible_fraction,cooking_yield,review_status\n"
        "7,苹果,带皮,0.8,0.7,approved\n",
        encoding="utf-8",
    )
    decisions = tmp_path / "decisions.csv"
    decisions.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,decision_form,edible_fraction,review_status\n"
        "1-1,1,7,苹果,带皮,带皮,0.65,modified\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="表头非法"):
        load_edible_fraction_rules(rules)
    index = load_edible_fraction_decisions(decisions)
    assert index.get_effective("1-1") is not None
