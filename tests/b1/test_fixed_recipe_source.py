"""T05 固定源守恒与逐行解析测试。

load_verified_recipe_source 必须先按 SourceManifest 核验源文件，再解析业务行；
recipe_id 严格连续 1..2000，全部行守恒，任何源变化在解析前抛
SOURCE_MANIFEST_MISMATCH。
"""

from pathlib import Path
from uuid import UUID

import pytest

from food_agent_v2.b1.consumer_views import (
    BuildIdentity,
    IngredientIdentityFact,
    IngredientOccurrenceFact,
    RecipeFact,
    build_consumer_views,
)
from food_agent_v2.b1.nutrition_occurrence_rules import (
    NutritionOccurrenceMetadata,
    load_nutrition_retention_decisions,
    load_nutrition_usage_decisions,
)
from food_agent_v2.b1.schemas import (
    SOURCE_BYTE_SIZE,
    SOURCE_ENCODING,
    SOURCE_HEADERS,
    SOURCE_ID,
    SOURCE_RELATIVE_PATH,
    SOURCE_ROW_COUNT,
    SOURCE_SHA256,
)
from food_agent_v2.b1.source_manifest import (
    canonical_build_input_manifest,
    load_verified_recipe_source,
)
from food_agent_v2.contracts.build import (
    SourceManifest,
    SourceManifestMismatch,
    source_manifest_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_CSV = REPO_ROOT / "data" / "raw" / "recipes_sample_2000.csv"


def canonical_manifest() -> SourceManifest:
    return SourceManifest(
        schema_version="1.0.0",
        source_id=SOURCE_ID,
        relative_path=SOURCE_RELATIVE_PATH,
        encoding=SOURCE_ENCODING,
        byte_size=SOURCE_BYTE_SIZE,
        sha256=SOURCE_SHA256,
        row_count=SOURCE_ROW_COUNT,
        headers=SOURCE_HEADERS,
    )


class TestFixedSource:
    def test_nonempty_nutrition_decisions_reach_fixed_consumer_projection(
        self, tmp_path: Path
    ) -> None:
        decision_path = tmp_path / "nutrition-usage.csv"
        decision_path.write_text(
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,usage_code,review_status\n"
            "1-1,1,10,鸡肉,,main,approved\n",
            encoding="utf-8",
        )
        current_occurrences = {
            "1-1": NutritionOccurrenceMetadata("1-1", 1, 10, "鸡肉", "")
        }
        usage_decisions = load_nutrition_usage_decisions(
            decision_path, current_occurrences=current_occurrences
        )
        retention_path = tmp_path / "nutrition-retention.csv"
        retention_path.write_text(
            "occurrence_id,recipe_id,ingredient_id,ingredient_name,normalized_form,retained_in_dish,review_status\n"
            "1-1,1,10,鸡肉,,true,approved\n",
            encoding="utf-8",
        )
        retention_decisions = load_nutrition_retention_decisions(
            retention_path, current_occurrences=current_occurrences
        )

        views = build_consumer_views(
            build=BuildIdentity(UUID("11111111-1111-1111-1111-111111111111"), "a" * 64),
            recipes=(RecipeFact(1, "固定菜品", "dish", ("加入鸡肉",)),),
            occurrences=(
                IngredientOccurrenceFact("1-1", 1, "鸡肉", "鸡肉", 10, "edible"),
            ),
            identities=(IngredientIdentityFact(10, "鸡肉", 1, category="肉禽"),),
            nutrition_usage_decisions=usage_decisions,
            nutrition_retention_decisions=retention_decisions,
        )

        nutrition_input = views.nutrition_views[0].ingredients[0]
        assert nutrition_input.usage_code == "main"
        assert nutrition_input.retained_in_dish is True
        assert nutrition_input.requires_review is False

    def test_h06_nutrition_review_inputs_are_hashed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        import food_agent_v2.b1.source_manifest as source_manifest_module

        manifest = canonical_build_input_manifest()
        paths = {entry["relative_path"] for entry in manifest["inputs"]}
        assert "data/review/ingredient_nutrition_usage_decisions.csv" in paths
        assert "data/review/ingredient_nutrition_retention_decisions.csv" in paths
        assert "data/review/ingredient_edible_fraction_decisions.csv" in paths

        for relative_path in paths:
            destination = tmp_path / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / relative_path, destination)

        monkeypatch.setattr(source_manifest_module, "PROJECT_ROOT", tmp_path)
        baseline = source_manifest_hash(source_manifest_module.canonical_build_input_manifest())

        usage_path = tmp_path / "data/review/ingredient_nutrition_usage_decisions.csv"
        usage_path.write_bytes(usage_path.read_bytes() + b"\n")
        usage_changed = source_manifest_hash(source_manifest_module.canonical_build_input_manifest())
        assert usage_changed != baseline

        retention_path = tmp_path / "data/review/ingredient_nutrition_retention_decisions.csv"
        retention_path.write_bytes(retention_path.read_bytes() + b"\n")
        retention_changed = source_manifest_hash(
            source_manifest_module.canonical_build_input_manifest()
        )
        assert retention_changed != usage_changed

        edible_path = tmp_path / "data/review/ingredient_edible_fraction_decisions.csv"
        edible_path.write_bytes(edible_path.read_bytes() + b"\n")
        edible_changed = source_manifest_hash(source_manifest_module.canonical_build_input_manifest())
        assert edible_changed != usage_changed

    def test_build_input_manifest_binds_reviewed_facts_but_not_model_cache(self) -> None:
        manifest = canonical_build_input_manifest()
        paths = {entry["relative_path"] for entry in manifest["inputs"]}

        assert "data/review/recipe_profile_enrichment.jsonl" in paths
        assert "data/review/ingredient_nutrition_retention_decisions.csv" in paths
        assert "data/review/ingredient_quantity_decisions.csv" in paths
        assert "data/review/ingredient_nutrition_crosswalk.jsonl" in paths
        assert "data/review/recipe_time_graph_decisions.csv" in paths
        assert "data/reference/ingredient_nutrition.jsonl" in paths
        assert "data/reference/usda_fooddata_central_manifest.json" in paths
        assert not any("data/cache" in path for path in paths)

    def test_loads_all_2000_rows(self) -> None:
        rows = load_verified_recipe_source(SOURCE_CSV, canonical_manifest())
        assert len(rows) == 2000

    def test_recipe_ids_continuous_1_to_2000(self) -> None:
        rows = load_verified_recipe_source(SOURCE_CSV, canonical_manifest())
        assert [r.recipe_id for r in rows] == list(range(1, 2001))

    def test_row_conservation_all_fields(self) -> None:
        rows = load_verified_recipe_source(SOURCE_CSV, canonical_manifest())
        for row in rows:
            assert row.source_row_number == row.recipe_id
            assert row.name
            assert isinstance(row.ingredients_raw, str)
            assert isinstance(row.steps_raw, str)
            assert isinstance(row.labels_raw, str)
            assert len(row.row_sha256) == 64

    def test_gbk_source_decodes_without_replacement_characters(self) -> None:
        rows = load_verified_recipe_source(SOURCE_CSV, canonical_manifest())
        text_fields = (
            value
            for row in rows
            for value in (row.name, row.ingredients_raw, row.steps_raw, row.labels_raw)
        )
        assert all("\ufffd" not in value for value in text_fields)
        assert rows[0].name == "秋梨膏"

    def test_one_byte_change_rejected_before_parse(self, tmp_path: Path) -> None:
        tampered = tmp_path / "recipes.csv"
        tampered.write_bytes(SOURCE_CSV.read_bytes())
        data = bytearray(tampered.read_bytes())
        data[1000] ^= 0x01
        tampered.write_bytes(bytes(data))

        with pytest.raises(SourceManifestMismatch) as excinfo:
            load_verified_recipe_source(tampered, canonical_manifest())
        assert excinfo.value.code == "SOURCE_MANIFEST_MISMATCH"
        assert "sha256" in str(excinfo.value)

    def test_missing_source_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SourceManifestMismatch) as excinfo:
            load_verified_recipe_source(tmp_path / "missing.csv", canonical_manifest())
        assert excinfo.value.code == "SOURCE_MANIFEST_MISMATCH"
