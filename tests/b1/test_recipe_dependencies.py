import json
import shutil
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

import pytest

from food_agent_v2.b1 import rebuild, review_inputs
from food_agent_v2.b1.consumer_views import (
    BuildIdentity,
    IngredientIdentityFact,
    IngredientOccurrenceFact,
    build_consumer_views,
    recipe_facts_from_source,
)
from food_agent_v2.b1.rag_document_builder import build_rag_documents_from_views
from food_agent_v2.b1.recipe_classifier import classify_all, load_overrides
from food_agent_v2.b1.review_inputs import RecipeDependency
from food_agent_v2.b1.schemas import RecipeClassification, RecordType, SourceRecipeRow
from food_agent_v2.b1.source_manifest import (
    canonical_build_input_manifest,
    canonical_source_manifest,
    load_verified_recipe_source,
)
from food_agent_v2.c1.qdrant_client import rag_document_payload
from food_agent_v2.contracts.build import source_manifest_hash
from food_agent_v2.core.paths import PROJECT_ROOT, RECIPES_RAW


def test_dependency_loader_uses_only_approved_decisions(tmp_path) -> None:
    path = tmp_path / "recipe_dependencies.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False)
            for record in (
                {
                    "parent_recipe_id": 10,
                    "dependency_recipe_id": 20,
                    "relation_type": "requires_component",
                    "review_status": "approved",
                },
                {
                    "parent_recipe_id": 10,
                    "dependency_recipe_id": 30,
                    "relation_type": "uses_program",
                    "review_status": "pending",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert hasattr(review_inputs, "load_recipe_dependencies")
    dependencies = review_inputs.load_recipe_dependencies(
        path,
        known_recipe_ids={10, 20, 30},
    )

    assert [asdict(item) for item in dependencies] == [
        {
            "parent_recipe_id": 10,
            "dependency_recipe_id": 20,
            "relation_type": "requires_component",
            "review_status": "approved",
        }
    ]


def test_dependency_loader_rejects_duplicate_active_edge(tmp_path) -> None:
    path = tmp_path / "recipe_dependencies.jsonl"
    record = {
        "parent_recipe_id": 10,
        "dependency_recipe_id": 20,
        "relation_type": "requires_component",
        "review_status": "approved",
    }
    path.write_text(
        json.dumps(record, ensure_ascii=False)
        + "\n"
        + json.dumps({**record, "review_status": "modified"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="重复"):
        review_inputs.load_recipe_dependencies(
            path,
            known_recipe_ids={10, 20},
        )


def test_dependency_loader_rejects_active_cycle(tmp_path) -> None:
    path = tmp_path / "recipe_dependencies.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "parent_recipe_id": parent,
                    "dependency_recipe_id": child,
                    "relation_type": "requires_component",
                    "review_status": "approved",
                },
                ensure_ascii=False,
            )
            for parent, child in ((10, 20), (20, 30), (30, 10))
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="环"):
        review_inputs.load_recipe_dependencies(
            path,
            known_recipe_ids={10, 20, 30},
        )


def test_dependency_loader_rejects_program_relation_to_dish(tmp_path) -> None:
    path = tmp_path / "recipe_dependencies.jsonl"
    path.write_text(
        json.dumps(
            {
                "parent_recipe_id": 10,
                "dependency_recipe_id": 20,
                "relation_type": "uses_program",
                "review_status": "approved",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="uses_program"):
        review_inputs.load_recipe_dependencies(
            path,
            known_recipe_ids={10, 20},
            record_types={10: "dish", 20: "dish"},
        )


def test_recipe_facts_resolve_dependency_ids_to_names() -> None:
    rows = (
        SourceRecipeRow(1, 1, "豆沙包", "豆沙馅100克", "包好蒸熟", "早餐", "1" * 64),
        SourceRecipeRow(2, 2, "豆沙馅", "红豆100克", "打成馅", "下午茶", "2" * 64),
    )
    classifications = (
        RecipeClassification(recipe_id=1, record_type=RecordType.DISH),
        RecipeClassification(recipe_id=2, record_type=RecordType.PREPARATION),
    )

    facts = recipe_facts_from_source(
        rows,
        classifications,
        recipe_dependencies=(
            RecipeDependency(1, 2, "requires_component", "approved"),
        ),
    )

    assert [asdict(item) for item in facts[0].dependencies] == [
        {
            "recipe_id": 2,
            "name": "豆沙馅",
            "relation_type": "requires_component",
        }
    ]
    assert facts[1].dependencies == ()


def test_dependency_is_projected_for_output_without_duplicate_nutrition() -> None:
    rows = (
        SourceRecipeRow(1, 1, "豆沙包", "豆沙馅100克", "包好蒸熟", "早餐", "1" * 64),
        SourceRecipeRow(2, 2, "豆沙馅", "红豆100克", "打成馅", "下午茶", "2" * 64),
    )
    classifications = (
        RecipeClassification(recipe_id=1, record_type=RecordType.DISH),
        RecipeClassification(recipe_id=2, record_type=RecordType.PREPARATION),
    )
    facts = recipe_facts_from_source(
        rows,
        classifications,
        recipe_dependencies=(
            RecipeDependency(1, 2, "requires_component", "approved"),
        ),
    )

    views = build_consumer_views(
        build=BuildIdentity(
            UUID("11111111-1111-1111-1111-111111111111"),
            "a" * 64,
        ),
        recipes=facts,
        occurrences=(
            IngredientOccurrenceFact("1-1", 1, "豆沙馅100克", "豆沙馅", 10, "edible"),
            IngredientOccurrenceFact("2-1", 2, "红豆100克", "红豆", 20, "edible"),
        ),
        identities=(
            IngredientIdentityFact(10, "豆沙馅", 10),
            IngredientIdentityFact(20, "红豆", 20),
        ),
    )

    assert views.recipe_ids == (1,)
    assert views.health_views[0].ingredient_ids == (10,)
    assert views.nutrition_views[0].ingredient_ids == (10,)
    assert views.step_views[0].dependency_recipe_ids == (2,)
    assert views.retrieval_views[0].dependency_recipe_ids == (2,)
    assert views.retrieval_views[0].dependency_names == ("豆沙馅",)
    assert views.retrieval_views[0].dependency_relation_types == (
        "requires_component",
    )

    documents, _ = build_rag_documents_from_views(views.retrieval_views)
    assert documents[0].dependency_recipe_ids == (2,)
    assert documents[0].dependency_names == ("豆沙馅",)
    assert "豆沙馅" in documents[0].searchable_text
    payload = rag_document_payload(documents[0].model_dump(mode="json"))
    assert payload["dependency_recipe_ids"] == [2]
    assert payload["dependency_names"] == ["豆沙馅"]
    assert payload["dependency_relation_types"] == ["requires_component"]


def test_rebuild_entry_returns_approved_dependency_records(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "profiles.jsonl"
    profile_path.write_text("", encoding="utf-8")
    condition_path = tmp_path / "conditions.csv"
    condition_path.write_text(
        "recipe_name,choice_group,selected_ingredient,retained_alternatives,review_status\n",
        encoding="utf-8",
    )
    dependency_path = tmp_path / "recipe_dependencies.jsonl"
    dependency_path.write_text(
        json.dumps(
            {
                "parent_recipe_id": 1,
                "dependency_recipe_id": 2,
                "relation_type": "requires_component",
                "review_status": "approved",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rebuild, "RECIPE_PROFILE_ENRICHMENTS", profile_path)
    monkeypatch.setattr(rebuild, "INGREDIENT_CONDITION_DEFAULTS", condition_path)
    monkeypatch.setattr(
        rebuild,
        "RECIPE_DEPENDENCIES",
        dependency_path,
        raising=False,
    )
    rows = (
        SourceRecipeRow(1, 1, "豆沙包", "豆沙馅100克", "包好蒸熟", "早餐", "1" * 64),
        SourceRecipeRow(2, 2, "豆沙馅", "红豆100克", "打成馅", "下午茶", "2" * 64),
    )
    classifications = (
        RecipeClassification(recipe_id=1, record_type=RecordType.DISH),
        RecipeClassification(recipe_id=2, record_type=RecordType.PREPARATION),
    )

    facts, reviewed_occurrences, dependencies = rebuild.prepare_reviewed_consumer_inputs(
        rows,
        classifications,
        (),
    )

    assert facts[0].dependencies[0].recipe_id == 2
    assert reviewed_occurrences == ()
    assert [asdict(item) for item in dependencies] == [
        {
            "parent_recipe_id": 1,
            "dependency_recipe_id": 2,
            "relation_type": "requires_component",
            "review_status": "approved",
        }
    ]


def test_reviewed_dependency_dataset_contains_known_recipe_requirements() -> None:
    rows = tuple(
        load_verified_recipe_source(RECIPES_RAW, canonical_source_manifest())
    )
    classifications = classify_all(
        list(rows),
        load_overrides(
            PROJECT_ROOT / "data" / "review" / "recipe_classification_overrides.csv"
        ),
    )
    dependencies = review_inputs.load_recipe_dependencies(
        PROJECT_ROOT / "data" / "review" / "recipe_dependencies.jsonl",
        known_recipe_ids={row.recipe_id for row in rows},
        record_types={
            item.recipe_id: item.record_type.value for item in classifications
        },
    )
    edges = {
        (
            item.parent_recipe_id,
            item.dependency_recipe_id,
            item.relation_type,
        )
        for item in dependencies
    }

    assert (1909, 1693, "requires_component") in edges  # 豆沙包 -> 豆沙馅
    assert (294, 1571, "requires_component") in edges  # 灌汤包 -> 皮冻
    assert (317, 993, "uses_program") in edges  # 草莓蛋糕 -> 奶油打发
    assert (1462, 298, "bundle_contains") in edges  # 套餐 -> 蒸米饭
    assert (77, 1131, "requires_component") in edges  # 统一引用虾干成品
    assert (77, 828, "requires_component") not in edges


@pytest.fixture(scope="module")
def full_dependency_build(tmp_path_factory):
    from food_agent_v2.b1.time_graph_profiler import TimeGraphCache

    staging = tmp_path_factory.mktemp("fixed-dependency-build")
    real_publisher = rebuild.publish_downstream_build_views
    cache = TimeGraphCache(PROJECT_ROOT / "data" / "cache" / "recipe_time_graphs.jsonl")

    def publish_with_approved_time_cache(*args, **kwargs):
        return real_publisher(
            *args,
            **kwargs,
            time_graph_cache=cache,
            time_graph_generator_model_id="deepseek-chat",
            time_graph_verifier_model_id="deepseek-chat",
        )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(rebuild, "publish_downstream_build_views", publish_with_approved_time_cache)
        report = rebuild.build_fixed_data_staging(
            staging,
            build_id=UUID("22222222-2222-2222-2222-222222222222"),
            builder_version="a" * 40,
        )
    manifest_path = Path(report["manifest_path"])
    return staging, report, json.loads(manifest_path.read_text(encoding="utf-8"))


def test_full_fixed_build_publishes_all_approved_recipe_dependencies(
    full_dependency_build,
) -> None:
    staging, report, manifest = full_dependency_build

    entry = manifest["artifacts"]["recipe_dependencies"]
    records = [
        json.loads(line)
        for line in (staging / entry["relative_path"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(records) == 167
    assert len({
        (
            row["parent_recipe_id"],
            row["dependency_recipe_id"],
            row["relation_type"],
            row["review_status"],
        )
        for row in records
    }) == 167
    assert sum(row["relation_type"] == "bundle_contains" for row in records) == 12
    assert {row["review_status"] for row in records} == {"approved"}
    assert {row["build_id"] for row in records} == {report["build_id"]}
    assert {row["source_manifest_hash"] for row in records} == {
        source_manifest_hash(canonical_build_input_manifest())
    }


def test_g18_rejects_missing_meal_bundle_dependency(
    full_dependency_build,
) -> None:
    from food_agent_v2.b1.quality_gates import DataQualityError, evaluate_staging_quality

    staging, report, manifest = full_dependency_build
    source_rows = [
        json.loads(line)
        for line in (staging / manifest["artifacts"]["recipe_source_rows"]["relative_path"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    classifications = [
        json.loads(line)
        for line in (staging / manifest["artifacts"]["recipe_classifications"]["relative_path"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    approved_dependencies = review_inputs.load_recipe_dependencies(
        PROJECT_ROOT / "data" / "review" / "recipe_dependencies.jsonl",
        known_recipe_ids={int(row["recipe_id"]) for row in source_rows},
        record_types={
            int(row["recipe_id"]): str(row["record_type"])
            for row in classifications
        },
    )
    removed = False
    tampered_records = []
    for dependency in approved_dependencies:
        if dependency.relation_type == "bundle_contains" and not removed:
            removed = True
            continue
        tampered_records.append(
            {
                **asdict(dependency),
                "build_id": report["build_id"],
                "source_manifest_hash": report["source_manifest_hash"],
            }
        )
    assert removed is True
    tampered_path = staging / "T07" / "recipe_dependencies_tampered.jsonl"
    tampered_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in tampered_records),
        encoding="utf-8",
    )
    artifact_paths = {
        name: staging / entry["relative_path"]
        for name, entry in manifest["artifacts"].items()
    }
    artifact_paths["recipe_dependencies"] = tampered_path

    with pytest.raises(DataQualityError) as caught:
        evaluate_staging_quality(
            artifact_paths,
            build_id=report["build_id"],
            source_manifest_hash=report["source_manifest_hash"],
        )
    assert caught.value.code == "G18_RECIPE_DEPENDENCY_CLOSURE"


def _g18_artifacts_with_one_modified_dependency(
    full_dependency_build,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Path], dict, tuple[int, int, str]]:
    from food_agent_v2.b1 import quality_gates

    staging, report, manifest = full_dependency_build
    review_dir = tmp_path / "data" / "review"
    review_dir.mkdir(parents=True)
    shutil.copyfile(
        PROJECT_ROOT / "data" / "review" / "recipe_profile_enrichment.jsonl",
        review_dir / "recipe_profile_enrichment.jsonl",
    )
    source_records = [
        json.loads(line)
        for line in (PROJECT_ROOT / "data" / "review" / "recipe_dependencies.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    modified_source = next(
        record for record in source_records if record["relation_type"] != "bundle_contains"
    )
    modified_source["review_status"] = "modified"
    (review_dir / "recipe_dependencies.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in source_records),
        encoding="utf-8",
    )

    dependency_entry = manifest["artifacts"]["recipe_dependencies"]
    artifact_records = [
        json.loads(line)
        for line in (staging / dependency_entry["relative_path"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    modified_artifact = next(
        record
        for record in artifact_records
        if (
            record["parent_recipe_id"],
            record["dependency_recipe_id"],
            record["relation_type"],
        )
        == (
            modified_source["parent_recipe_id"],
            modified_source["dependency_recipe_id"],
            modified_source["relation_type"],
        )
    )
    modified_artifact["review_status"] = "modified"
    artifact_path = tmp_path / "recipe_dependencies_modified.jsonl"
    artifact_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in artifact_records),
        encoding="utf-8",
    )
    artifact_paths = {
        name: staging / entry["relative_path"]
        for name, entry in manifest["artifacts"].items()
    }
    artifact_paths["recipe_dependencies"] = artifact_path
    monkeypatch.setattr(quality_gates, "PROJECT_ROOT", tmp_path)
    return (
        artifact_paths,
        report,
        (
            modified_artifact["parent_recipe_id"],
            modified_artifact["dependency_recipe_id"],
            modified_artifact["relation_type"],
        ),
    )


def test_g18_accepts_modified_review_authorized_dependency(
    full_dependency_build,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from food_agent_v2.b1.quality_gates import evaluate_staging_quality

    artifact_paths, report, _ = _g18_artifacts_with_one_modified_dependency(
        full_dependency_build,
        tmp_path,
        monkeypatch,
    )

    evaluate_staging_quality(
        artifact_paths,
        build_id=report["build_id"],
        source_manifest_hash=report["source_manifest_hash"],
    )


def test_g18_rejects_missing_modified_review_authorized_dependency(
    full_dependency_build,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from food_agent_v2.b1.quality_gates import DataQualityError, evaluate_staging_quality

    artifact_paths, report, modified_key = _g18_artifacts_with_one_modified_dependency(
        full_dependency_build,
        tmp_path,
        monkeypatch,
    )
    records = [
        json.loads(line)
        for line in artifact_paths["recipe_dependencies"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    artifact_paths["recipe_dependencies"].write_text(
        "".join(
            json.dumps(record, ensure_ascii=False) + "\n"
            for record in records
            if (
                record["parent_recipe_id"],
                record["dependency_recipe_id"],
                record["relation_type"],
            )
            != modified_key
        ),
        encoding="utf-8",
    )

    with pytest.raises(DataQualityError) as caught:
        evaluate_staging_quality(
            artifact_paths,
            build_id=report["build_id"],
            source_manifest_hash=report["source_manifest_hash"],
        )
    assert caught.value.code == "G18_RECIPE_DEPENDENCY_CLOSURE"


def test_g18_rejects_altered_modified_review_authorized_dependency(
    full_dependency_build,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from food_agent_v2.b1.quality_gates import DataQualityError, evaluate_staging_quality

    artifact_paths, report, modified_key = _g18_artifacts_with_one_modified_dependency(
        full_dependency_build,
        tmp_path,
        monkeypatch,
    )
    records = [
        json.loads(line)
        for line in artifact_paths["recipe_dependencies"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for record in records:
        if (
            record["parent_recipe_id"],
            record["dependency_recipe_id"],
            record["relation_type"],
        ) == modified_key:
            record["review_status"] = "approved"
    artifact_paths["recipe_dependencies"].write_text(
        "".join(
            json.dumps(record, ensure_ascii=False) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )

    with pytest.raises(DataQualityError) as caught:
        evaluate_staging_quality(
            artifact_paths,
            build_id=report["build_id"],
            source_manifest_hash=report["source_manifest_hash"],
        )
    assert caught.value.code == "G18_RECIPE_DEPENDENCY_CLOSURE"
