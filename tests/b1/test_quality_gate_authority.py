import json
from pathlib import Path

import pytest

from food_agent_v2.b1 import quality_gates
from food_agent_v2.b1.step_atomizer import atomize_recipe_steps

_BUILD_ID = "11111111-1111-1111-1111-111111111111"
_MANIFEST_HASH = "a" * 64


def _record(**values: object) -> dict:
    return {
        **values,
        "build_id": _BUILD_ID,
        "source_manifest_hash": _MANIFEST_HASH,
    }


def _valid_artifacts(
    labels_raw: str = "晚餐",
    *,
    label_tags: list[str] | None = None,
) -> dict[str, list[dict]]:
    downstream_label_tags = label_tags or ["晚餐"]
    source_rows = [
        _record(
            recipe_id=recipe_id,
            source_row_number=recipe_id,
            name=f"菜{recipe_id}",
            ingredients_raw="水100克",
            steps_raw="煮1分钟",
            labels_raw=labels_raw,
            row_sha256=f"{recipe_id:064x}"[-64:],
        )
        for recipe_id in range(1, 2001)
    ]
    eligible_ids = range(1, 1933)
    task_rows = []
    for recipe_id in eligible_ids:
        atom = atomize_recipe_steps(
            recipe_id=recipe_id,
            steps=((1, "煮1分钟"),),
        )[0]
        task_rows.append(
            _record(
                recipe_id=recipe_id,
                recipe_name=f"菜{recipe_id}",
                active_seconds=60,
                estimated_elapsed_seconds=60,
                step_tasks=[
                    {
                        "atom_id": atom.atom_id,
                        "text": atom.text,
                        "duration_seconds": 60,
                        "task_type": "manual",
                        "resources": ["cook"],
                        "depends_on": [],
                    }
                ],
            )
        )
    artifacts = {
        "recipe_source_rows": source_rows,
        "recipe_classifications": [
            _record(
                recipe_id=recipe_id,
                record_type="meal_bundle" if recipe_id <= 12 else "dish",
                review_status="approved",
            )
            for recipe_id in range(1, 2001)
        ],
        "user_profiles": [_record(user_id=user_id) for user_id in range(1, 51)],
        "ingredient_occurrences": [],
        "ingredient_registry": [_record(ingredient_id=1, name_canonical="水")],
        "ingredient_aliases": [],
        "ingredient_forms": [],
        "ingredient_crosswalk": [],
        "recipe_ingredient_relations": [],
        "recipe_health_views": [
            _record(recipe_id=recipe_id, ingredient_ids=[1])
            for recipe_id in eligible_ids
        ],
        "recipe_step_binding_views": [
            _record(recipe_id=recipe_id, ingredient_ids=[1], steps=[{"step_index": 1}])
            for recipe_id in eligible_ids
        ],
        "recipe_nutrition_input_views": [
            _record(recipe_id=recipe_id, ingredient_ids=[1])
            for recipe_id in eligible_ids
        ],
        "recipe_retrieval_build_views": [
            _record(
                recipe_id=recipe_id,
                label_tags=list(downstream_label_tags),
                meal_tags=["晚餐"],
            )
            for recipe_id in eligible_ids
        ],
        "step_tasks": task_rows,
        "nutrition_features": [
            _record(
                recipe_id=recipe_id,
                available=False,
                reason="fixture unavailable",
                raw_edible_input_weight_g=None,
                raw_nutrition_total=None,
                raw_nutrition_per_100g=None,
            )
            for recipe_id in eligible_ids
        ],
        "rag_documents": [
            _record(
                recipe_id=recipe_id,
                label_tags=list(downstream_label_tags),
                meal_tags=["晚餐"],
                searchable_fields={"meal": "晚餐"},
            )
            for recipe_id in eligible_ids
        ],
        "health_relation_decisions": [
            _record(
                constraint_code=code,
                ingredient_id=1,
                review_status="approved",
                decision="allow",
            )
            for code in quality_gates.ALLOWED_CONSTRAINT_CODES
        ],
        "health_relations": [],
        "health_relation_coverage": [
            _record(
                constraint_code=code,
                coverage_status="complete",
                universe_ingredient_count=1,
                reviewed_ingredient_count=1,
            )
            for code in quality_gates.ALLOWED_CONSTRAINT_CODES
        ],
        "recipe_dependencies": [
            _record(
                parent_recipe_id=parent_recipe_id,
                dependency_recipe_id=dependency_recipe_id,
                relation_type=relation_type,
                review_status="approved",
            )
            for parent_recipe_id, dependency_recipe_id, relation_type in (
                [
                    (recipe_id, 100 + recipe_id, "bundle_contains")
                    for recipe_id in range(1, 13)
                ]
                + [
                    (200 + index, 500 + index, "requires_component")
                    for index in range(155)
                ]
            )
        ],
    }
    assert set(artifacts) == set(quality_gates.REQUIRED_ARTIFACTS)
    return artifacts


def _evaluate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifacts: dict[str, list[dict]],
    enrichment: dict[str, object] | None = None,
) -> None:
    enrichment_path = tmp_path / "data" / "review" / "recipe_profile_enrichment.jsonl"
    enrichment_path.parent.mkdir(parents=True, exist_ok=True)
    enrichment_path.write_text(
        "" if enrichment is None else json.dumps(enrichment, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    dependency_path = tmp_path / "data" / "review" / "recipe_dependencies.jsonl"
    dependency_path.write_text(
        "".join(
            json.dumps(
                {
                    "parent_recipe_id": record["parent_recipe_id"],
                    "dependency_recipe_id": record["dependency_recipe_id"],
                    "relation_type": record["relation_type"],
                    "review_status": record["review_status"],
                },
                ensure_ascii=False,
            )
            + "\n"
            for record in artifacts["recipe_dependencies"]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(quality_gates, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        quality_gates,
        "TIME_REVIEW_DECISIONS",
        tmp_path / "unit-fixture-without-time-decisions.csv",
    )
    monkeypatch.setattr(
        quality_gates,
        "_read_jsonl",
        lambda path: artifacts[Path(path).name],
    )
    quality_gates.evaluate_staging_quality(
        {name: Path(name) for name in quality_gates.REQUIRED_ARTIFACTS},
        build_id=_BUILD_ID,
        source_manifest_hash=_MANIFEST_HASH,
    )


def test_g12_rejects_paired_meal_corruption_when_raw_meal_is_empty(
    monkeypatch,
    tmp_path,
) -> None:
    artifacts = _valid_artifacts()
    artifacts["recipe_source_rows"][0]["labels_raw"] = ""
    approved_enrichment = {
        "recipe_id": 1,
        "meal_tags": ["午餐"],
        "review_status": "approved",
    }
    artifacts["recipe_retrieval_build_views"][0]["label_tags"] = []
    artifacts["rag_documents"][0]["label_tags"] = []
    artifacts["recipe_retrieval_build_views"][0]["meal_tags"] = ["午餐"]
    artifacts["rag_documents"][0]["meal_tags"] = ["午餐"]

    _evaluate(monkeypatch, tmp_path, artifacts, enrichment=approved_enrichment)

    artifacts["recipe_retrieval_build_views"][0]["meal_tags"] = ["晚餐"]
    artifacts["rag_documents"][0]["meal_tags"] = ["晚餐"]

    with pytest.raises(quality_gates.DataQualityError) as caught:
        _evaluate(monkeypatch, tmp_path, artifacts, enrichment=approved_enrichment)

    assert caught.value.code == "G12_RAG_LABEL_AND_MEAL_COVERAGE"


def test_g12_rejects_paired_raw_label_loss(monkeypatch, tmp_path) -> None:
    artifacts = _valid_artifacts(
        labels_raw="晚餐,清淡",
        label_tags=["晚餐", "清淡"],
    )

    _evaluate(monkeypatch, tmp_path, artifacts)

    artifacts["recipe_retrieval_build_views"][0]["label_tags"] = ["晚餐"]
    artifacts["rag_documents"][0]["label_tags"] = ["晚餐"]

    with pytest.raises(quality_gates.DataQualityError) as caught:
        _evaluate(monkeypatch, tmp_path, artifacts)

    assert caught.value.code == "G12_RAG_LABEL_AND_MEAL_COVERAGE"


def test_g12_accepts_modified_enrichment_when_raw_meal_is_empty(
    monkeypatch,
    tmp_path,
) -> None:
    artifacts = _valid_artifacts()
    artifacts["recipe_source_rows"][0]["labels_raw"] = ""
    modified_enrichment = {
        "recipe_id": 1,
        "meal_tags": ["午餐"],
        "review_status": "modified",
    }
    artifacts["recipe_retrieval_build_views"][0]["label_tags"] = []
    artifacts["rag_documents"][0]["label_tags"] = []
    artifacts["recipe_retrieval_build_views"][0]["meal_tags"] = ["午餐"]
    artifacts["rag_documents"][0]["meal_tags"] = ["午餐"]

    _evaluate(monkeypatch, tmp_path, artifacts, enrichment=modified_enrichment)


def test_g12_rejects_pending_enrichment_when_raw_meal_is_empty(
    monkeypatch,
    tmp_path,
) -> None:
    artifacts = _valid_artifacts()
    artifacts["recipe_source_rows"][0]["labels_raw"] = ""
    pending_enrichment = {
        "recipe_id": 1,
        "meal_tags": ["午餐"],
        "review_status": "pending",
    }
    artifacts["recipe_retrieval_build_views"][0]["label_tags"] = []
    artifacts["rag_documents"][0]["label_tags"] = []
    artifacts["recipe_retrieval_build_views"][0]["meal_tags"] = ["午餐"]
    artifacts["rag_documents"][0]["meal_tags"] = ["午餐"]

    with pytest.raises(quality_gates.DataQualityError) as caught:
        _evaluate(monkeypatch, tmp_path, artifacts, enrichment=pending_enrichment)

    assert caught.value.code == "G12_RAG_LABEL_AND_MEAL_COVERAGE"


def test_g12_rejects_downstream_meal_tags_that_extend_raw_meals(
    monkeypatch,
    tmp_path,
) -> None:
    artifacts = _valid_artifacts()
    artifacts["recipe_retrieval_build_views"][0]["meal_tags"] = ["晚餐", "早餐"]
    artifacts["rag_documents"][0]["meal_tags"] = ["晚餐", "早餐"]

    with pytest.raises(quality_gates.DataQualityError) as caught:
        _evaluate(monkeypatch, tmp_path, artifacts)

    assert caught.value.code == "G12_RAG_LABEL_AND_MEAL_COVERAGE"


def test_g16_rejects_equal_count_atom_substitution(monkeypatch, tmp_path) -> None:
    artifacts = _valid_artifacts()
    artifacts["step_tasks"][0]["step_tasks"][0]["atom_id"] = "substituted-atom"

    with pytest.raises(quality_gates.DataQualityError) as caught:
        _evaluate(monkeypatch, tmp_path, artifacts)

    assert caught.value.code == "G16_TIME_GRAPH_COMPLETE_AND_ACYCLIC"


def test_g16_rejects_locked_explicit_duration_tampering(monkeypatch, tmp_path) -> None:
    artifacts = _valid_artifacts()
    artifacts["step_tasks"][0]["step_tasks"][0]["duration_seconds"] = 120

    with pytest.raises(quality_gates.DataQualityError) as caught:
        _evaluate(monkeypatch, tmp_path, artifacts)

    assert caught.value.code == "G16_TIME_GRAPH_COMPLETE_AND_ACYCLIC"
