from decimal import Decimal
from uuid import UUID

from food_agent_v2.b1.consumer_views import (
    NutritionOccurrenceInput,
    RecipeNutritionInputView,
)
from food_agent_v2.b1.nutrition_feature_builder import (
    build_nutrition_features_from_views,
)
from food_agent_v2.b1.nutrition_reference import (
    NutritionCrosswalkIndex,
    NutritionReferenceIndex,
)
from food_agent_v2.b1.quantity_normalizer import (
    EdibleFractionRule,
    EdibleFractionRuleIndex,
    MeasureRuleIndex,
    QuantityDecisionIndex,
)


def _view() -> RecipeNutritionInputView:
    return RecipeNutritionInputView(
        build_id=UUID("11111111-1111-1111-1111-111111111111"),
        source_manifest_hash="a" * 64,
        recipe_id=1,
        ingredients=(
            NutritionOccurrenceInput(
                occurrence_id="1-1",
                ingredient_id=10,
                quantity_raw="100克",
                unit_raw="克",
                quantity_status="explicit",
                ingredient_name="姜",
            ),
        ),
    )


def test_unavailable_recipe_is_still_published_without_partial_totals() -> None:
    features, report = build_nutrition_features_from_views(
        (_view(),),
        measure_rules=MeasureRuleIndex(()),
        quantity_decisions=QuantityDecisionIndex(()),
        edible_fractions=EdibleFractionRuleIndex(
            (EdibleFractionRule("姜", "", Decimal("1"), "approved"),)
        ),
        nutrition_crosswalk=NutritionCrosswalkIndex((), NutritionReferenceIndex(())),
    )

    assert len(features) == 1
    assert features[0].available is False
    assert features[0].reason == "mapping_missing"
    assert features[0].raw_edible_input_weight_g is None
    assert features[0].raw_nutrition_total is None
    assert features[0].raw_nutrition_per_100g is None
    assert report["total_recipes"] == 1
    assert report["unavailable_count"] == 1
    assert report["reason_counts"] == {"mapping_missing": 1}


def test_missing_edible_fraction_is_not_silently_treated_as_one() -> None:
    features, report = build_nutrition_features_from_views(
        (_view(),),
        measure_rules=MeasureRuleIndex(()),
        quantity_decisions=QuantityDecisionIndex(()),
        edible_fractions=EdibleFractionRuleIndex(()),
        nutrition_crosswalk=NutritionCrosswalkIndex((), NutritionReferenceIndex(())),
    )

    assert features[0].reason == "edible_fraction_missing"
    assert report["reason_counts"] == {"edible_fraction_missing": 1}
