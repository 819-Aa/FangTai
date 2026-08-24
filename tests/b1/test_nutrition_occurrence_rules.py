from pathlib import Path
from typing import get_type_hints

import pytest

from food_agent_v2.b1.consumer_views import (
    IngredientIdentityFact,
    IngredientOccurrenceFact,
    StructuredStep,
)
from food_agent_v2.b1.nutrition_occurrence_rules import (
    NutritionRetentionDecision,
    NutritionRetentionDecisionIndex,
    NutritionRetentionResolution,
    NutritionUsageDecision,
    NutritionUsageDecisionIndex,
    NutritionUsageResolution,
    classify_fuzzy_token,
    derive_nutrition_retention,
    derive_nutrition_usage,
    load_nutrition_retention_decisions,
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


def _usage(
    occurrence_id: str = "1-1", review_status: str = "approved"
) -> NutritionUsageDecision:
    return NutritionUsageDecision(
        occurrence_id, 1, 10, "盐", "", "seasoning", review_status  # type: ignore[arg-type]
    )


def _retention(
    occurrence_id: str = "1-1", review_status: str = "approved"
) -> NutritionRetentionDecision:
    return NutritionRetentionDecision(
        occurrence_id, 1, 10, "盐", "", False, review_status  # type: ignore[arg-type]
    )


def test_seasoning_resolves_usage_but_not_retention() -> None:
    usage = derive_nutrition_usage(
        occurrence=_occurrence(),
        identity=_identity(category="调料"),
        steps=_bound_steps("撒盐调味"),
        decisions=NutritionUsageDecisionIndex(()),
    )
    retention = derive_nutrition_retention(
        occurrence=_occurrence(),
        identity=_identity(category="调料"),
        steps=_bound_steps("撒盐调味"),
        decisions=NutritionRetentionDecisionIndex(()),
    )
    assert usage == NutritionUsageResolution("seasoning", False)
    assert retention == NutritionRetentionResolution(None, True)


@pytest.mark.parametrize("name", ["油", "高汤"])
def test_controlled_oils_and_liquids_do_not_use_seasoning_default(name: str) -> None:
    usage = derive_nutrition_usage(
        occurrence=_occurrence(name=name),
        identity=_identity(category="调料", name=name),
        steps=_bound_steps(f"加入{name}"),
        decisions=NutritionUsageDecisionIndex(()),
    )
    assert usage == NutritionUsageResolution(None, True)


def test_usage_and_retention_csvs_have_independent_active_rows(tmp_path: Path) -> None:
    usage_path = tmp_path / "usage.csv"
    usage_path.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,review_status\n"
        "1-1,1,10,盐,,seasoning,approved\n",
        encoding="utf-8",
    )
    retention_path = tmp_path / "retention.csv"
    retention_path.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,retained_in_dish,review_status\n"
        "1-1,1,10,盐,,false,modified\n",
        encoding="utf-8",
    )
    assert load_nutrition_usage_decisions(usage_path).get_effective("1-1").usage_code == "seasoning"
    assert load_nutrition_retention_decisions(retention_path).get_effective("1-1").retained_in_dish is False


@pytest.mark.parametrize(
    ("loader", "header"),
    [
        (load_nutrition_usage_decisions, "occurrence_id,recipe_id,usage_code,review_status"),
        (load_nutrition_retention_decisions, "occurrence_id,recipe_id,retained_in_dish,review_status"),
    ],
)
def test_decision_loaders_reject_illegal_headers(loader, header: str, tmp_path: Path) -> None:
    path = tmp_path / "decisions.csv"
    path.write_text(header + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="表头非法"):
        loader(path)


@pytest.mark.parametrize(
    ("index", "first", "second"),
    [
        (NutritionUsageDecisionIndex, _usage("1-1", "pending"), _usage("1-1", "approved")),
        (NutritionRetentionDecisionIndex, _retention("1-1", "rejected"), _retention("1-1", "modified")),
    ],
)
def test_decision_indices_reject_duplicate_occurrences_including_inactive_rows(index, first, second) -> None:
    with pytest.raises(ValueError, match="重复"):
        index((first, second))


@pytest.mark.parametrize(
    ("loader", "header", "row"),
    [
        (
            load_nutrition_usage_decisions,
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,review_status",
            "9-9,9,10,盐,,main,pending",
        ),
        (
            load_nutrition_retention_decisions,
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,retained_in_dish,review_status",
            "9-9,9,10,盐,,false,rejected",
        ),
    ],
)
def test_decision_loaders_reject_unknown_occurrences_for_every_status(loader, header: str, row: str, tmp_path: Path) -> None:
    path = tmp_path / "decisions.csv"
    path.write_text(f"{header}\n{row}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未知 occurrence_id"):
        loader(path, current_occurrence_ids={"1-1"})


@pytest.mark.parametrize(
    ("index", "decision", "message"),
    [
        (NutritionUsageDecisionIndex, NutritionUsageDecision("1-1", 1, 10, "盐", "", "invalid", "approved"), "usage_code"),  # type: ignore[arg-type]
        (NutritionRetentionDecisionIndex, NutritionRetentionDecision("1-1", 1, 10, "盐", "", False, "invalid"), "review_status"),  # type: ignore[arg-type]
    ],
)
def test_decision_indices_reject_illegal_enums(index, decision, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        index((decision,))


def test_retention_loader_rejects_illegal_boolean(tmp_path: Path) -> None:
    path = tmp_path / "retention.csv"
    path.write_text(
        "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,retained_in_dish,review_status\n"
        "1-1,1,10,盐,,yes,approved\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="retained_in_dish"):
        load_nutrition_retention_decisions(path)


@pytest.mark.parametrize(
    ("resolver", "index"),
    [
        (derive_nutrition_usage, NutritionUsageDecisionIndex((_usage(),))),
        (derive_nutrition_retention, NutritionRetentionDecisionIndex((_retention(),))),
    ],
)
def test_effective_decisions_require_exact_metadata(resolver, index) -> None:
    occurrence = IngredientOccurrenceFact("1-1", 1, "盐", "盐", 11, "edible")
    resolution = resolver(
        occurrence=occurrence,
        identity=IngredientIdentityFact(11, "盐", 1, category="调料"),
        steps=_bound_steps("撒盐调味"),
        decisions=index,
    )
    assert resolution.requires_review is True


def test_effective_usage_and_retention_decisions_resolve_independently() -> None:
    usage = derive_nutrition_usage(
        occurrence=_occurrence(),
        identity=_identity(category="调料"),
        steps=(),
        decisions=NutritionUsageDecisionIndex((_usage(),)),
    )
    retention = derive_nutrition_retention(
        occurrence=_occurrence(),
        identity=_identity(category="调料"),
        steps=(),
        decisions=NutritionRetentionDecisionIndex((_retention(),)),
    )
    assert usage == NutritionUsageResolution("seasoning", False)
    assert retention == NutritionRetentionResolution(False, False)


def test_fuzzy_token_uses_only_local_evidence_and_explicit_amount_wins() -> None:
    assert classify_fuzzy_token("少许", "撒少许盐") == "small_amount"
    assert classify_fuzzy_token("100克", "盐少许") is None


def test_nutrition_occurrence_input_combines_review_dimensions() -> None:
    from food_agent_v2.b1.consumer_views import NutritionOccurrenceInput

    input_view = NutritionOccurrenceInput(
        occurrence_id="1-1",
        ingredient_id=10,
        quantity_raw=None,
        unit_raw=None,
        quantity_status="unknown",
        usage_requires_review=False,
        retention_requires_review=True,
    )
    assert input_view.requires_review is True
    assert get_type_hints(NutritionOccurrenceInput)["usage_code"] is not None
