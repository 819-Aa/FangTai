import pytest

from food_agent_v2.b1.consumer_views import (
    IngredientIdentityFact,
    IngredientOccurrenceFact,
    StructuredStep,
)
from food_agent_v2.b1.nutrition_occurrence_rules import (
    NutritionUsageDecision,
    NutritionUsageDecisionIndex,
    classify_fuzzy_token,
    derive_nutrition_usage,
    load_nutrition_usage_decisions,
)


def _occurrence(*, occurrence_id: str = "1-1", name: str = "盐") -> IngredientOccurrenceFact:
    return IngredientOccurrenceFact(
        occurrence_id=occurrence_id,
        recipe_id=1,
        source_fragment=name,
        name_clean=name,
        ingredient_id=10,
        consumption_role="edible",
    )


def _identity(*, category: str, name: str = "盐") -> IngredientIdentityFact:
    return IngredientIdentityFact(
        ingredient_id=10,
        name_canonical=name,
        family_id=1,
        category=category,
    )


def _bound_steps(text: str, occurrence_id: str = "1-1") -> tuple[StructuredStep, ...]:
    return (
        StructuredStep(
            step_index=1,
            raw_text=text,
            bound_occurrence_ids=(occurrence_id,),
            bound_ingredient_ids=(10,),
            binding_methods=("exact_id",),
        ),
    )


def test_fuzzy_token_and_usage_are_closed_and_deterministic() -> None:
    assert classify_fuzzy_token("少许", "撒少许盐") == "small_amount"
    assert classify_fuzzy_token("几滴", "滴入香油") == "few_drops"

    resolution = derive_nutrition_usage(
        occurrence=_occurrence(),
        identity=_identity(category="调料"),
        steps=_bound_steps("撒少许盐调味"),
        decisions=NutritionUsageDecisionIndex(()),
    )

    assert resolution.usage_code == "seasoning"
    assert resolution.retained_in_dish is True
    assert resolution.requires_review is False


@pytest.mark.parametrize(
    ("category", "step", "usage_code", "retained_in_dish"),
    [
        ("肉禽", "炒熟鸡肉", "main", True),
        ("蔬菜", "加入葱花拌匀", "supporting", True),
        ("调料", "热锅下油", "cooking_fat", True),
        ("调料", "加入高汤煮开", "retained_liquid", True),
    ],
)
def test_category_and_bound_step_resolve_each_usage_code(
    category: str, step: str, usage_code: str, retained_in_dish: bool
) -> None:
    resolution = derive_nutrition_usage(
        occurrence=_occurrence(
            name="鸡肉" if category == "肉禽" else "高汤" if usage_code == "retained_liquid" else "油"
        ),
        identity=_identity(
            category=category,
            name="鸡肉" if category == "肉禽" else "高汤" if usage_code == "retained_liquid" else "油",
        ),
        steps=_bound_steps(step),
        decisions=NutritionUsageDecisionIndex(()),
    )

    assert resolution.usage_code == usage_code
    assert resolution.retained_in_dish is retained_in_dish
    assert resolution.requires_review is False


def test_ambiguous_usage_requires_an_effective_occurrence_decision() -> None:
    occurrence = _occurrence(name="水")
    identity = _identity(category="其他", name="水")
    missing = derive_nutrition_usage(
        occurrence=occurrence,
        identity=identity,
        steps=(),
        decisions=NutritionUsageDecisionIndex(()),
    )
    decided = derive_nutrition_usage(
        occurrence=occurrence,
        identity=identity,
        steps=(),
        decisions=NutritionUsageDecisionIndex(
            (
                NutritionUsageDecision(
                    occurrence_id="1-1",
                    usage_code="retained_liquid",
                    retained_in_dish=True,
                    review_status="modified",
                ),
            )
        ),
    )

    assert missing.usage_code is None
    assert missing.retained_in_dish is None
    assert missing.requires_review is True
    assert decided.usage_code == "retained_liquid"
    assert decided.retained_in_dish is True
    assert decided.requires_review is False


def test_decision_index_rejects_duplicate_effective_decisions_and_illegal_enums() -> None:
    decision = NutritionUsageDecision("1-1", "main", True, "approved")
    with pytest.raises(ValueError, match="重复"):
        NutritionUsageDecisionIndex((decision, decision))
    with pytest.raises(ValueError, match="usage_code"):
        NutritionUsageDecisionIndex(
            (NutritionUsageDecision("1-1", "garnish", True, "approved"),)
        )
    with pytest.raises(ValueError, match="review_status"):
        NutritionUsageDecisionIndex(
            (NutritionUsageDecision("1-1", "main", True, "autopassed"),)
        )


def test_usage_decision_loader_keeps_only_approved_or_modified_rows_effective(tmp_path) -> None:
    path = tmp_path / "nutrition-usage.csv"
    path.write_text(
        "occurrence_id,usage_code,retained_in_dish,review_status\n"
        "1-1,seasoning,true,approved\n"
        "1-2,main,true,pending\n",
        encoding="utf-8",
    )

    decisions = load_nutrition_usage_decisions(path)

    assert decisions.get_effective("1-1").usage_code == "seasoning"
    assert decisions.get_effective("1-2") is None
