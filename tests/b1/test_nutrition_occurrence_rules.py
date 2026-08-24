from typing import get_type_hints

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


def test_late_added_oil_does_not_inherit_heating_from_another_clause() -> None:
    resolution = derive_nutrition_usage(
        occurrence=_occurrence(name="油"),
        identity=_identity(category="调料", name="油"),
        steps=_bound_steps("鸡肉炒熟后关火，淋入油拌匀"),
        decisions=NutritionUsageDecisionIndex(()),
    )

    assert resolution.usage_code is None
    assert resolution.retained_in_dish is None
    assert resolution.requires_review is True


def test_liquid_without_occurrence_decision_requires_review_even_when_step_mentions_adding() -> None:
    resolution = derive_nutrition_usage(
        occurrence=_occurrence(name="高汤"),
        identity=_identity(category="调料", name="高汤"),
        steps=_bound_steps("加入高汤煮开"),
        decisions=NutritionUsageDecisionIndex(()),
    )

    assert resolution.usage_code is None
    assert resolution.retained_in_dish is None
    assert resolution.requires_review is True


@pytest.mark.parametrize(("category", "name"), [("肉禽", "鸡肉"), ("蔬菜", "葱花")])
def test_category_alone_does_not_guess_main_or_supporting(category: str, name: str) -> None:
    resolution = derive_nutrition_usage(
        occurrence=_occurrence(name=name),
        identity=_identity(category=category, name=name),
        steps=_bound_steps(f"加入{name}"),
        decisions=NutritionUsageDecisionIndex(()),
    )

    assert resolution.usage_code is None
    assert resolution.requires_review is True


@pytest.mark.parametrize("name", ["生抽", "蚝油"])
def test_sauce_with_heating_evidence_is_not_cooking_fat(name: str) -> None:
    resolution = derive_nutrition_usage(
        occurrence=_occurrence(name=name),
        identity=_identity(category="调料", name=name),
        steps=_bound_steps(f"热锅下{name}翻炒"),
        decisions=NutritionUsageDecisionIndex(()),
    )

    assert resolution.usage_code == "seasoning"
    assert resolution.requires_review is False


def test_controlled_oil_without_heating_evidence_requires_review() -> None:
    resolution = derive_nutrition_usage(
        occurrence=_occurrence(name="油"),
        identity=_identity(category="调料", name="油"),
        steps=_bound_steps("加入油拌匀"),
        decisions=NutritionUsageDecisionIndex(()),
    )

    assert resolution.usage_code is None
    assert resolution.requires_review is True


@pytest.mark.parametrize("step", ["焯水后倒掉", "浸泡后沥干", "过滤后弃去汤汁"])
def test_liquid_discard_language_does_not_replace_occurrence_decision(step: str) -> None:
    resolution = derive_nutrition_usage(
        occurrence=_occurrence(name="高汤"),
        identity=_identity(category="调料", name="高汤"),
        steps=_bound_steps(step),
        decisions=NutritionUsageDecisionIndex(()),
    )

    assert resolution.usage_code is None
    assert resolution.retained_in_dish is None
    assert resolution.requires_review is True


@pytest.mark.parametrize(
    ("raw", "fragment", "expected"),
    [
        ("适量", "盐适量", "as_needed"),
        ("少许", "盐少许", "small_amount"),
        ("数个", "红枣数个", "several_count"),
        ("几滴", "香油几滴", "few_drops"),
        ("100克", "盐少许", None),
        ("适中", "适中", None),
    ],
)
def test_fuzzy_token_uses_only_local_evidence_and_explicit_amount_wins(
    raw: str, fragment: str, expected: str | None
) -> None:
    assert classify_fuzzy_token(raw, fragment) == expected


def test_fuzzy_token_does_not_leak_from_another_ingredient_in_shared_step() -> None:
    assert classify_fuzzy_token("100克", "盐100克") is None


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
                    recipe_id=1,
                    ingredient_id=10,
                    ingredient_name="水",
                    normalized_form="",
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
    decision = NutritionUsageDecision("1-1", 1, 10, "盐", "", "main", True, "approved")
    with pytest.raises(ValueError, match="重复"):
        NutritionUsageDecisionIndex((decision, decision))
    with pytest.raises(ValueError, match="usage_code"):
        NutritionUsageDecisionIndex(
            (NutritionUsageDecision("1-1", 1, 10, "盐", "", "garnish", True, "approved"),)
        )
    with pytest.raises(ValueError, match="review_status"):
        NutritionUsageDecisionIndex(
            (NutritionUsageDecision("1-1", 1, 10, "盐", "", "main", True, "autopassed"),)
        )


@pytest.mark.parametrize(("first_status", "second_status"), [("pending", "pending"), ("approved", "rejected")])
def test_decision_index_rejects_duplicate_occurrence_ids_across_all_statuses(
    first_status: str, second_status: str
) -> None:
    with pytest.raises(ValueError, match="重复"):
        NutritionUsageDecisionIndex(
            (
                NutritionUsageDecision("1-1", 1, 10, "盐", "", "main", True, first_status),  # type: ignore[arg-type]
                NutritionUsageDecision("1-1", 1, 10, "盐", "", "main", True, second_status),  # type: ignore[arg-type]
            )
        )


def test_usage_decision_loader_keeps_only_approved_or_modified_rows_effective(tmp_path) -> None:
    path = tmp_path / "nutrition-usage.csv"
    path.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,retained_in_dish,review_status\n"
        "1-1,1,10,盐,,seasoning,true,approved\n"
        "1-2,1,20,葱,,main,true,pending\n",
        encoding="utf-8",
    )

    decisions = load_nutrition_usage_decisions(path)

    assert decisions.get_effective("1-1").usage_code == "seasoning"
    assert decisions.get_effective("1-2") is None


def test_usage_decision_rejects_invalid_boolean_and_metadata_drift(tmp_path) -> None:
    path = tmp_path / "nutrition-usage.csv"
    path.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,retained_in_dish,review_status\n"
        "1-1,1,10,盐,粉,seasoning,yes,approved\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="retained_in_dish"):
        load_nutrition_usage_decisions(path)

    drifted = NutritionUsageDecisionIndex(
        (NutritionUsageDecision("1-1", 1, 11, "食用油", "", "cooking_fat", True, "approved"),)
    )
    resolution = derive_nutrition_usage(
        occurrence=IngredientOccurrenceFact(
            "1-1", 1, "食用油", "食用油", 10, "edible"
        ),
        identity=_identity(category="调料", name="食用油"),
        steps=_bound_steps("热锅下食用油"),
        decisions=drifted,
    )
    assert resolution.requires_review is True


def test_usage_loader_rejects_unknown_occurrence_ids_for_any_review_status(tmp_path) -> None:
    path = tmp_path / "nutrition-usage.csv"
    path.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,retained_in_dish,review_status\n"
        "9-9,9,10,盐,,main,true,pending\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="未知 occurrence_id"):
        load_nutrition_usage_decisions(path, current_occurrence_ids={"1-1"})


def test_effective_decision_is_applied_only_when_metadata_matches() -> None:
    decision = NutritionUsageDecision(
        "1-1", 1, 10, "鸡肉", "块", "main", True, "approved"
    )
    resolution = derive_nutrition_usage(
        occurrence=IngredientOccurrenceFact(
            "1-1", 1, "鸡肉块", "鸡肉", 10, "edible", form="块"
        ),
        identity=_identity(category="肉禽", name="鸡肉"),
        steps=_bound_steps("加入鸡肉块"),
        decisions=NutritionUsageDecisionIndex((decision,)),
    )
    assert resolution.usage_code == "main"
    assert resolution.requires_review is False


@pytest.mark.parametrize("review_status", ["approved", "modified"])
def test_matching_effective_decision_resolves_cooking_fat(review_status: str) -> None:
    decision = NutritionUsageDecision(
        "1-1", 1, 10, "油", "", "cooking_fat", True, review_status  # type: ignore[arg-type]
    )
    resolution = derive_nutrition_usage(
        occurrence=_occurrence(name="油"),
        identity=_identity(category="调料", name="油"),
        steps=_bound_steps("鸡肉炒熟后关火，淋入油拌匀"),
        decisions=NutritionUsageDecisionIndex((decision,)),
    )

    assert resolution.usage_code == "cooking_fat"
    assert resolution.retained_in_dish is True
    assert resolution.requires_review is False


def test_nutrition_occurrence_input_type_hints_are_resolvable() -> None:
    from food_agent_v2.b1.consumer_views import NutritionOccurrenceInput

    hints = get_type_hints(NutritionOccurrenceInput)
    assert hints["usage_code"] is not None
