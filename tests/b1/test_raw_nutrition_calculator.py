from decimal import Decimal
from uuid import UUID

import pytest

from food_agent_v2.b1.consumer_views import (
    NutritionOccurrenceInput,
    RecipeNutritionInputView,
)
from food_agent_v2.b1.nutrition_calculator import calculate_raw_recipe_nutrition
from food_agent_v2.b1.nutrition_reference import (
    IngredientNutritionReference,
    NutritionCrosswalkDecision,
    NutritionCrosswalkIndex,
    NutritionReferenceIndex,
    NutritionVector,
)
from food_agent_v2.b1.quantity_normalizer import (
    EdibleFractionRule,
    EdibleFractionRuleIndex,
    MeasureRuleIndex,
    QuantityDecisionIndex,
)


def _view(*ingredients: NutritionOccurrenceInput) -> RecipeNutritionInputView:
    return RecipeNutritionInputView(
        build_id=UUID("11111111-1111-1111-1111-111111111111"),
        source_manifest_hash="a" * 64,
        recipe_id=1,
        ingredients=ingredients,
    )


def _ingredient(
    occurrence_id: str,
    ingredient_id: int,
    name: str,
    raw: str,
    *,
    retained_in_dish: bool | None = True,
    requires_review: bool = False,
):
    return NutritionOccurrenceInput(
        occurrence_id=occurrence_id,
        ingredient_id=ingredient_id,
        quantity_raw=raw,
        unit_raw="克",
        quantity_status="explicit",
        ingredient_name=name,
        retained_in_dish=retained_in_dish,
        requires_review=requires_review,
    )


def _vector(value: str) -> NutritionVector:
    number = Decimal(value)
    return NutritionVector(**{field: number for field in NutritionVector.model_fields})


def _references(*pairs: tuple[int, str, str]):
    references = NutritionReferenceIndex(
        tuple(
            IngredientNutritionReference(
                reference_id=reference_id,
                canonical_name=name,
                form="unspecified",
                per_100g=_vector(value),
                source_name="权威来源",
                source_url=f"https://example.invalid/{reference_id}",
                source_dataset="china_cdc",
            )
            for _ingredient_id, name, reference_id, value in pairs
        )
    )
    crosswalk = NutritionCrosswalkIndex(
        tuple(
            NutritionCrosswalkDecision(
                ingredient_id=ingredient_id,
                ingredient_name=name,
                form="unspecified",
                reference_id=reference_id,
                match_method="exact",
                review_status="approved",
            )
            for ingredient_id, name, reference_id, _value in pairs
        ),
        references,
    )
    return crosswalk


def test_two_ingredient_decimal_calculation_is_all_raw_input_and_rounds_only_at_output() -> None:
    view = _view(
        _ingredient("1-1", 10, "甲", "100克"),
        _ingredient("1-2", 20, "乙", "50克"),
    )
    edible = EdibleFractionRuleIndex(
        (
            EdibleFractionRule("甲", "", Decimal("0.8"), "approved"),
            EdibleFractionRule("乙", "", Decimal("1"), "approved"),
        )
    )

    result = calculate_raw_recipe_nutrition(
        view,
        measure_rules=MeasureRuleIndex(()),
        quantity_decisions=QuantityDecisionIndex(()),
        edible_fractions=edible,
        nutrition_crosswalk=_references(
            (10, "甲", "ref-a", "10"),
            (20, "乙", "ref-b", "20"),
        ),
    )

    assert result.available is True
    assert result.raw_edible_input_weight_g == Decimal("130.00")
    assert result.raw_nutrition_total.energy_kcal == Decimal("18.00")
    assert result.raw_nutrition_per_100g.energy_kcal == Decimal("13.85")
    dumped = result.model_dump(mode="json")
    assert "source_name" not in str(dumped)
    assert "confidence" not in str(dumped)


@pytest.mark.parametrize(
    ("raw", "with_fraction", "with_mapping", "complete", "reason"),
    [
        ("少许", True, True, True, "quantity_unapproved"),
        ("100克", False, True, True, "edible_fraction_missing"),
        ("100克", True, False, True, "mapping_missing"),
        ("100克", True, True, False, "nutrient_incomplete"),
    ],
)
def test_any_missing_prerequisite_returns_no_partial_nutrition(
    raw, with_fraction, with_mapping, complete, reason
) -> None:
    view = _view(_ingredient("1-1", 10, "甲", raw))
    edible = EdibleFractionRuleIndex(
        (EdibleFractionRule("甲", "", Decimal("1"), "approved"),)
        if with_fraction
        else ()
    )
    if with_mapping:
        references = NutritionReferenceIndex(
            (
                IngredientNutritionReference(
                    reference_id="ref-a",
                    canonical_name="甲",
                    form="unspecified",
                    per_100g=(
                        _vector("10")
                        if complete
                        else NutritionVector(energy_kcal=Decimal("10"))
                    ),
                    source_name="权威来源",
                    source_url="https://example.invalid/ref-a",
                    source_dataset="china_cdc",
                ),
            )
        )
        crosswalk = NutritionCrosswalkIndex(
            (
                NutritionCrosswalkDecision(
                    ingredient_id=10,
                    ingredient_name="甲",
                    form="unspecified",
                    reference_id="ref-a",
                    match_method="exact",
                    review_status="approved",
                ),
            ),
            references,
        )
    else:
        crosswalk = NutritionCrosswalkIndex((), NutritionReferenceIndex(()))

    result = calculate_raw_recipe_nutrition(
        view,
        measure_rules=MeasureRuleIndex(()),
        quantity_decisions=QuantityDecisionIndex(()),
        edible_fractions=edible,
        nutrition_crosswalk=crosswalk,
    )

    assert result.available is False
    assert result.reason == reason
    assert result.raw_edible_input_weight_g is None
    assert result.raw_nutrition_total is None
    assert result.raw_nutrition_per_100g is None


def test_review_required_occurrence_makes_nutrition_unavailable() -> None:
    view = _view(_ingredient("1-1", 10, "甲", "100克", requires_review=True))

    result = calculate_raw_recipe_nutrition(
        view,
        measure_rules=MeasureRuleIndex(()),
        quantity_decisions=QuantityDecisionIndex(()),
        edible_fractions=EdibleFractionRuleIndex(()),
        nutrition_crosswalk=NutritionCrosswalkIndex((), NutritionReferenceIndex(())),
    )

    assert result.available is False
    assert result.reason == "usage_unresolved"


def test_non_retained_occurrence_does_not_contribute_to_raw_nutrition() -> None:
    view = _view(
        _ingredient("1-1", 10, "甲", "100克"),
        _ingredient("1-2", 20, "乙", "50克", retained_in_dish=False),
    )
    result = calculate_raw_recipe_nutrition(
        view,
        measure_rules=MeasureRuleIndex(()),
        quantity_decisions=QuantityDecisionIndex(()),
        edible_fractions=EdibleFractionRuleIndex(
            (EdibleFractionRule("甲", "", Decimal("1"), "approved"),)
        ),
        nutrition_crosswalk=_references((10, "甲", "ref-a", "10")),
    )

    assert result.available is True
    assert result.raw_edible_input_weight_g == Decimal("100.00")
    assert result.raw_nutrition_total.energy_kcal == Decimal("10.00")
