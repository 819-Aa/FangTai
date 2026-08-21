import json
from dataclasses import replace
from uuid import UUID

import pytest

from food_agent_v2.b1.consumer_views import (
    BuildIdentity,
    IngredientIdentityFact,
    IngredientOccurrenceFact,
    RecipeFact,
    build_consumer_views,
    identity_facts_from_records,
    occurrence_facts_from_records,
    publish_consumer_views,
    publish_downstream_build_views,
    recipe_facts_from_source,
)
from food_agent_v2.b1.ingredient_identity import rebuild_ingredient_identities
from food_agent_v2.b1.rag_document_builder import build_rag_documents_from_views
from food_agent_v2.b1.recipe_classifier import classify_all, load_overrides
from food_agent_v2.b1.source_manifest import (
    canonical_source_manifest,
    load_verified_recipe_source,
)
from food_agent_v2.contracts.build import source_manifest_hash
from food_agent_v2.core.paths import FOOD_COMPOSITION, PROJECT_ROOT, RECIPES_RAW

BUILD = BuildIdentity(
    build_id=UUID("11111111-1111-1111-1111-111111111111"),
    source_manifest_hash="a" * 64,
)


def test_all_consumer_views_share_build_recipe_and_ingredient_identity() -> None:
    views = build_consumer_views(
        build=BUILD,
        recipes=(
            RecipeFact(
                recipe_id=1,
                name="姜丝拌菜",
                record_type="dish",
                step_segments=("姜丝与盐拌匀",),
                searchable_fields={"taste": "清淡"},
            ),
        ),
        occurrences=(
            IngredientOccurrenceFact(
                occurrence_id="1-1",
                recipe_id=1,
                source_fragment="姜丝5克",
                name_clean="姜",
                ingredient_id=10,
                consumption_role="edible",
                quantity_raw="5克",
                unit_raw="克",
                form="丝",
            ),
            IngredientOccurrenceFact(
                occurrence_id="1-2",
                recipe_id=1,
                source_fragment="盐2克",
                name_clean="盐",
                ingredient_id=20,
                consumption_role="edible",
                quantity_raw="2克",
                unit_raw="克",
            ),
        ),
        identities=(
            IngredientIdentityFact(10, "姜", 1, ("生姜",)),
            IngredientIdentityFact(20, "盐", 2),
        ),
    )

    assert views.recipe_ids == (1,)
    assert views.build == BUILD
    assert views.health_views[0].ingredient_ids == (10, 20)
    assert views.nutrition_views[0].ingredient_ids == (10, 20)
    assert views.retrieval_views[0].ingredient_ids == (10, 20)
    assert views.step_views[0].ingredient_ids == (10, 20)
    assert views.step_views[0].steps[0].bound_occurrence_ids == ("1-1", "1-2")
    assert {
        view.build_id
        for view in (
            views.health_views[0],
            views.nutrition_views[0],
            views.retrieval_views[0],
            views.step_views[0],
        )
    } == {BUILD.build_id}


def test_non_dish_never_enters_retrieval_documents() -> None:
    views = build_consumer_views(
        build=BUILD,
        recipes=(
            RecipeFact(1, "清蒸鱼", "dish", ("蒸10分钟",)),
            RecipeFact(2, "低温慢煮", "cooking_program", ("设置低温模式",)),
        ),
        occurrences=(
            IngredientOccurrenceFact("1-1", 1, "鱼500克", "鱼", 1, "edible"),
            IngredientOccurrenceFact("2-1", 2, "水", "水", 2, "edible"),
        ),
        identities=(
            IngredientIdentityFact(1, "鱼", 1),
            IngredientIdentityFact(2, "水", 2),
        ),
    )

    assert views.recipe_ids == (1,)
    documents, report = build_rag_documents_from_views(views.retrieval_views)
    assert [document.recipe_id for document in documents] == [1]
    assert report["total_documents"] == 1
    assert "health" not in documents[0].model_dump_json().lower()
    assert "nutrition" not in documents[0].model_dump_json().lower()


def test_non_edible_material_stays_in_steps_without_polluting_food_views() -> None:
    views = build_consumer_views(
        build=BUILD,
        recipes=(RecipeFact(1, "荷叶蒸鸡", "dish", ("用棉绳固定后蒸熟",)),),
        occurrences=(
            IngredientOccurrenceFact("1-1", 1, "鸡500克", "鸡", 1, "edible"),
            IngredientOccurrenceFact("1-2", 1, "棉绳1根", "棉绳", None, "non_edible"),
        ),
        identities=(IngredientIdentityFact(1, "鸡", 1),),
    )

    assert views.recipe_ids == (1,)
    assert views.health_views[0].ingredient_ids == (1,)
    assert views.nutrition_views[0].ingredient_ids == (1,)
    assert views.retrieval_views[0].ingredient_ids == (1,)
    assert views.step_views[0].steps[0].bound_occurrence_ids == ("1-2",)


def test_conditioned_ingredients_project_different_consumer_sets() -> None:
    views = build_consumer_views(
        build=BUILD,
        recipes=(RecipeFact(1, "条件菜", "dish", ("处理全部食材",)),),
        occurrences=(
            IngredientOccurrenceFact("1-1", 1, "豆腐", "豆腐", 10, "edible"),
            IngredientOccurrenceFact(
                "1-2", 1, "香菜可选", "香菜", 20, "edible",
                condition_type="optional", selected_for_base=False,
            ),
            IngredientOccurrenceFact(
                "1-3", 1, "牛肩肉或牛腩", "牛肩肉", 30, "edible",
                condition_type="one_of", choice_group_id="meat", selected_for_base=True,
            ),
            IngredientOccurrenceFact(
                "1-4", 1, "牛肩肉或牛腩", "牛腩", 40, "edible",
                condition_type="one_of", choice_group_id="meat", selected_for_base=False,
            ),
        ),
        identities=(
            IngredientIdentityFact(10, "豆腐", 1),
            IngredientIdentityFact(20, "香菜", 2),
            IngredientIdentityFact(30, "牛肩肉", 3),
            IngredientIdentityFact(40, "牛腩", 3),
        ),
    )

    assert views.health_views[0].ingredient_ids == (10, 20, 30)
    assert views.nutrition_views[0].ingredient_ids == (10, 30)
    assert views.retrieval_views[0].ingredient_ids == (10, 20, 30, 40)
    assert [relation.condition_type for relation in views.health_views[0].ingredient_relations] == [
        "required", "optional", "one_of",
    ]


def test_publish_rejects_cross_build_consumer_view(tmp_path) -> None:
    views = build_consumer_views(
        build=BUILD,
        recipes=(RecipeFact(1, "清蒸鱼", "dish", ("蒸10分钟",)),),
        occurrences=(IngredientOccurrenceFact("1-1", 1, "鱼500克", "鱼", 1, "edible"),),
        identities=(IngredientIdentityFact(1, "鱼", 1),),
    )
    tampered = replace(
        views,
        health_views=(
            replace(
                views.health_views[0],
                build_id=UUID("99999999-9999-9999-9999-999999999999"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="构建身份"):
        publish_consumer_views(tampered, tmp_path)


def test_overlapping_names_use_longest_exact_step_binding() -> None:
    views = build_consumer_views(
        build=BUILD,
        recipes=(RecipeFact(1, "鸡肉调味", "dish", ("加入鸡精拌匀",)),),
        occurrences=(
            IngredientOccurrenceFact("1-1", 1, "鸡500克", "鸡", 1, "edible"),
            IngredientOccurrenceFact("1-2", 1, "鸡精2克", "鸡精", 2, "edible"),
        ),
        identities=(
            IngredientIdentityFact(1, "鸡", 1),
            IngredientIdentityFact(2, "鸡精", 2),
        ),
    )

    assert views.step_views[0].steps[0].bound_occurrence_ids == ("1-2",)
    assert views.step_views[0].steps[0].bound_ingredient_ids == (2,)


def test_projection_failure_leaves_no_partial_view_files(tmp_path) -> None:
    views = build_consumer_views(
        build=BUILD,
        recipes=(RecipeFact(1, "清蒸鱼", "dish", ("蒸10分钟",)),),
        occurrences=(IngredientOccurrenceFact("1-1", 1, "鱼500克", "鱼", 1, "edible"),),
        identities=(IngredientIdentityFact(1, "鱼", 1),),
    )
    tampered = replace(
        views,
        nutrition_views=(replace(views.nutrition_views[0], ingredients=()),),
    )

    with pytest.raises(ValueError, match="食材身份"):
        publish_consumer_views(tampered, tmp_path)
    assert not list(tmp_path.glob("*.jsonl"))


def test_real_fixed_source_publishes_consistent_views(tmp_path) -> None:
    rows = tuple(load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest()))
    classifications = tuple(
        classify_all(
            list(rows),
            load_overrides(
                PROJECT_ROOT / "data" / "review" / "recipe_classification_overrides.csv"
            ),
        )
    )
    t06_dir = tmp_path / "T06"
    rebuild_ingredient_identities(
        list(rows),
        PROJECT_ROOT / "data" / "review" / "ingredient_identity_overrides.csv",
        t06_dir,
    )

    def read_jsonl(name: str) -> tuple[dict, ...]:
        return tuple(
            json.loads(line)
            for line in (t06_dir / name).read_text(encoding="utf-8").splitlines()
            if line
        )

    identities = identity_facts_from_records(
        read_jsonl("ingredient_registry.jsonl"),
        read_jsonl("ingredient_aliases.jsonl"),
    )
    views = build_consumer_views(
        build=BuildIdentity(
            UUID("22222222-2222-2222-2222-222222222222"),
            source_manifest_hash(canonical_source_manifest()),
        ),
        recipes=recipe_facts_from_source(rows, classifications),
        occurrences=occurrence_facts_from_records(read_jsonl("ingredient_occurrences.jsonl")),
        identities=identities,
    )
    staging = tmp_path / "T07"
    report = publish_consumer_views(views, staging)
    downstream = publish_downstream_build_views(
        views,
        identities,
        tuple(
            json.loads(line)
            for line in FOOD_COMPOSITION.read_text(encoding="utf-8").splitlines()
            if line
        ),
        staging,
    )

    assert len(views.recipe_ids) == 1914
    assert report["status"] == "passed"
    assert report["eligible_recipe_count"] == 1914
    assert report["ingredient_consistency_error_count"] == 0
    assert set(report["view_counts"].values()) == {1914}
    assert downstream["status"] == "passed"
    assert set(downstream["derived_counts"].values()) == {1914}
    assert (
        downstream["nutrition"]["available_count"] + downstream["nutrition"]["unavailable_count"]
        == 1914
    )
    assert downstream["nutrition"]["mapped_unique_ingredient_count"] == 131
    assert downstream["nutrition"]["unique_ingredient_count"] == 1733
    assert downstream["nutrition"]["mapping_coverage"] == 0.0756
    assert downstream["nutrition"]["recipes_with_any_reference"] == 1367
    assert "zero_complete_recipe_coverage" in downstream["nutrition"]["warnings"]
    assert (staging / "rag_documents.jsonl").exists()
