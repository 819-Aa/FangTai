import json

import pytest

from food_agent_v2.b1 import rebuild
from food_agent_v2.b1.consumer_views import IngredientOccurrenceFact, recipe_facts_from_source
from food_agent_v2.b1.review_inputs import (
    RecipeProfileEnrichment,
    apply_condition_defaults,
    load_ingredient_condition_defaults,
    load_recipe_profile_enrichments,
    write_profile_candidates,
)
from food_agent_v2.b1.schemas import RecipeClassification, RecordType, SourceRecipeRow


def _row(recipe_id: int, labels: str) -> SourceRecipeRow:
    return SourceRecipeRow(
        recipe_id=recipe_id,
        source_row_number=recipe_id,
        name=f"菜{recipe_id}",
        ingredients_raw="豆腐100克",
        steps_raw="蒸熟",
        labels_raw=labels,
        row_sha256=str(recipe_id) * 64,
    )


def _classification(recipe_id: int) -> RecipeClassification:
    return RecipeClassification(recipe_id=recipe_id, record_type=RecordType.DISH)


def test_raw_labels_are_fully_projected_into_structured_facets() -> None:
    facts = recipe_facts_from_source(
        (_row(1, "晚餐、老人、清淡"),),
        (_classification(1),),
    )

    fact = facts[0]
    assert fact.label_tags == ("晚餐", "老人", "清淡")
    assert fact.meal_tags == ("晚餐",)
    assert fact.population_tags == ("老人",)
    assert fact.taste_tags == ("清淡",)
    assert fact.source_row_sha256 == "1" * 64


def test_approved_non_sensitive_enrichment_fills_missing_facets() -> None:
    enrichment = RecipeProfileEnrichment(
        recipe_id=1,
        meal_tags=("早餐",),
        dish_type_tags=("主食",),
        cooking_method_tags=("蒸",),
        texture_tags=("松软",),
        review_status="approved",
    )
    facts = recipe_facts_from_source(
        (_row(1, ""),),
        (_classification(1),),
        profile_enrichments={1: enrichment},
    )

    assert facts[0].meal_tags == ("早餐",)
    assert facts[0].dish_type_tags == ("主食",)
    assert facts[0].cooking_method_tags == ("蒸",)


def test_sensitive_population_tag_cannot_be_generated_without_raw_label() -> None:
    enrichment = RecipeProfileEnrichment(
        recipe_id=1,
        population_tags=("老人",),
        review_status="approved",
    )

    with pytest.raises(ValueError, match="敏感标签"):
        recipe_facts_from_source(
            (_row(1, "晚餐"),),
            (_classification(1),),
            profile_enrichments={1: enrichment},
        )


def test_profile_loader_rejects_duplicate_recipe_decisions(tmp_path) -> None:
    path = tmp_path / "profiles.jsonl"
    record = {"recipe_id": 1, "meal_tags": ["晚餐"], "review_status": "approved"}
    path.write_text(
        json.dumps(record, ensure_ascii=False) + "\n" + json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="重复"):
        load_recipe_profile_enrichments(path, known_recipe_ids={1})


def test_pending_profile_is_not_loaded_as_effective_fact(tmp_path) -> None:
    path = tmp_path / "profiles.jsonl"
    path.write_text(
        json.dumps(
            {"recipe_id": 1, "meal_tags": ["晚餐"], "review_status": "pending"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    assert load_recipe_profile_enrichments(path, known_recipe_ids={1}) == {}


def test_approved_condition_default_selects_exactly_one_alternative(tmp_path) -> None:
    path = tmp_path / "conditions.csv"
    path.write_text(
        "recipe_name,choice_group,selected_ingredient,retained_alternatives,review_status\n"
        "条件菜,牛肩肉/牛腩,牛肩肉,牛肩肉|牛腩,approved\n",
        encoding="utf-8",
    )
    decisions = load_ingredient_condition_defaults(path, known_recipe_names={"条件菜"})
    occurrences = (
        IngredientOccurrenceFact(
            "1-1", 1, "牛肩肉或牛腩", "牛肩肉", 30, "edible",
            choice_group_id="1", condition_type="one_of",
        ),
        IngredientOccurrenceFact(
            "1-2", 1, "牛肩肉或牛腩", "牛腩", 40, "edible",
            choice_group_id="1", condition_type="one_of",
        ),
    )

    selected = apply_condition_defaults(
        occurrences,
        recipe_names={1: "条件菜"},
        decisions=decisions,
    )

    assert [item.selected_for_base for item in selected] == [True, False]


def test_profile_candidate_writer_never_auto_approves(tmp_path) -> None:
    fact = recipe_facts_from_source(
        (_row(1, "晚餐、老人"),),
        (_classification(1),),
    )[0]
    output = tmp_path / "candidates.jsonl"

    write_profile_candidates((fact,), output)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["meal_tags"] == ["晚餐"]
    assert payload["population_tags"] == ["老人"]
    assert payload["review_status"] == "pending"


def test_rebuild_entry_applies_reviewed_profiles_and_condition_defaults(
    tmp_path, monkeypatch
) -> None:
    profile_path = tmp_path / "profiles.jsonl"
    profile_path.write_text(
        json.dumps(
            {"recipe_id": 1, "meal_tags": ["晚餐"], "review_status": "approved"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    condition_path = tmp_path / "conditions.csv"
    condition_path.write_text(
        "recipe_name,choice_group,selected_ingredient,retained_alternatives,review_status\n"
        "条件菜,牛肩肉/牛腩,牛肩肉,牛肩肉|牛腩,approved\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rebuild, "RECIPE_PROFILE_ENRICHMENTS", profile_path)
    monkeypatch.setattr(rebuild, "INGREDIENT_CONDITION_DEFAULTS", condition_path)
    row = SourceRecipeRow(
        recipe_id=1,
        source_row_number=1,
        name="条件菜",
        ingredients_raw="牛肩肉或牛腩",
        steps_raw="炖熟",
        labels_raw="",
        row_sha256="1" * 64,
    )
    occurrences = (
        IngredientOccurrenceFact(
            "1-1", 1, "牛肩肉或牛腩", "牛肩肉", 30, "edible",
            choice_group_id="1", condition_type="one_of",
        ),
        IngredientOccurrenceFact(
            "1-2", 1, "牛肩肉或牛腩", "牛腩", 40, "edible",
            choice_group_id="1", condition_type="one_of",
        ),
    )

    facts, reviewed_occurrences = rebuild.prepare_reviewed_consumer_inputs(
        (row,), (_classification(1),), occurrences
    )

    assert facts[0].meal_tags == ("晚餐",)
    assert [item.selected_for_base for item in reviewed_occurrences] == [True, False]
