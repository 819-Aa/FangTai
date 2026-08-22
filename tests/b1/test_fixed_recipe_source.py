"""T05 固定源守恒与逐行解析测试。

load_verified_recipe_source 必须先按 SourceManifest 核验源文件，再解析业务行；
recipe_id 严格连续 1..2000，全部行守恒，任何源变化在解析前抛
SOURCE_MANIFEST_MISMATCH。
"""

from pathlib import Path

import pytest

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
from food_agent_v2.contracts.build import SourceManifest, SourceManifestMismatch

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
    def test_build_input_manifest_binds_reviewed_facts_but_not_model_cache(self) -> None:
        manifest = canonical_build_input_manifest()
        paths = {entry["relative_path"] for entry in manifest["inputs"]}

        assert "data/review/recipe_profile_enrichment.jsonl" in paths
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
