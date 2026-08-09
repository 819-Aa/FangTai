from uuid import UUID

from food_agent_v2.b1.consumer_views import (
    IngredientIdentityFact,
    NutritionOccurrenceInput,
    RecipeNutritionInputView,
)
from food_agent_v2.b1.nutrition_feature_builder import (
    build_nutrition_features_from_views,
    index_food_composition,
)


def _view(*ingredient_ids: int) -> RecipeNutritionInputView:
    return RecipeNutritionInputView(
        build_id=UUID("11111111-1111-1111-1111-111111111111"),
        source_manifest_hash="a" * 64,
        recipe_id=1,
        ingredients=tuple(
            NutritionOccurrenceInput(
                occurrence_id=f"1-{index}",
                ingredient_id=ingredient_id,
                quantity_raw="100克",
                unit_raw="克",
                quantity_status="explicit",
            )
            for index, ingredient_id in enumerate(ingredient_ids, start=1)
        ),
    )


def test_explicit_per_100g_reference_preserves_source_and_units() -> None:
    references = index_food_composition(
        (
            {
                "source_id": 1,
                "name": "姜",
                "energy_kcal_per_100g": 46.0,
                "protein_g_per_100g": 1.3,
                "source_name": "固定营养参考",
                "source_url": "https://example.invalid/1",
            },
        )
    )
    features, report = build_nutrition_features_from_views(
        (_view(10),),
        (IngredientIdentityFact(10, "姜", 1),),
        references,
    )

    assert report["available_count"] == 1
    assert features[0].available is True
    assert features[0].reason == "complete_reference_coverage"
    values = {item.nutrient_code: item for item in features[0].references[0].nutrients}
    assert values["energy_kcal"].value == 46.0
    assert values["energy_kcal"].unit == "kcal"
    assert values["energy_kcal"].basis == "per_100g"
    assert values["energy_kcal"].source_name == "固定营养参考"


def test_missing_mapping_is_unavailable_and_never_returns_placeholder_score() -> None:
    features, report = build_nutrition_features_from_views(
        (_view(10, 20),),
        (
            IngredientIdentityFact(10, "姜", 1),
            IngredientIdentityFact(20, "不存在于参考表", 2),
        ),
        index_food_composition(
            (
                {
                    "source_id": 1,
                    "name": "姜",
                    "energy_kcal_per_100g": 46.0,
                    "source_name": "固定营养参考",
                    "source_url": "https://example.invalid/1",
                },
            )
        ),
    )

    assert report["unavailable_count"] == 1
    assert report["unique_ingredient_count"] == 2
    assert report["mapped_unique_ingredient_count"] == 1
    assert report["mapping_coverage"] == 0.5
    assert report["recipes_with_any_reference"] == 1
    assert "zero_complete_recipe_coverage" in report["warnings"]
    assert features[0].available is False
    assert features[0].reason == "missing_reference_mapping"
    assert features[0].missing_ingredient_ids == (20,)
    dumped = features[0].model_dump()
    assert "score" not in dumped
    assert "coverage_ratio" not in dumped
    assert 0.5 not in dumped.values()
