"""固定源加载入口（T05）。

B1 唯一接受原始菜品源的入口。先按 SourceManifest 逐项核验
（存在性/byte_size/sha256/编码/表头/2,000 行），再逐行解析为
SourceRecipeRow，recipe_id 严格等于行号 1..2000。任何源事实不匹配在
读取业务行之前即抛 SOURCE_MANIFEST_MISMATCH。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from food_agent_v2.b1.schemas import (
    SOURCE_BYTE_SIZE,
    SOURCE_ENCODING,
    SOURCE_HEADERS,
    SOURCE_ID,
    SOURCE_RELATIVE_PATH,
    SOURCE_ROW_COUNT,
    SOURCE_SHA256,
    SourceRecipeRow,
)
from food_agent_v2.contracts.build import SourceManifest, verify_source_file
from food_agent_v2.core.paths import PROJECT_ROOT

_HEADER_INDEX = {"名称": 0, "食材清单": 1, "烹饪步骤": 2, "label": 3}

_BUILD_INPUT_PATHS = (
    "data/raw/recipes_sample_2000.csv",
    "data/raw/50个用户健康档案_详细版7.13.json",
    "data/review/recipe_classification_overrides.csv",
    "data/review/ingredient_identity_overrides.csv",
    "data/review/health_relation_decisions.csv",
    "data/review/recipe_profile_enrichment.jsonl",
    "data/review/ingredient_condition_defaults.csv",
    "data/review/ingredient_measure_rules.csv",
    "data/review/ingredient_edible_fraction_rules.csv",
    "data/review/ingredient_quantity_decisions.csv",
    "data/review/ingredient_nutrition_usage_decisions.csv",
    "data/review/ingredient_edible_fraction_decisions.csv",
    "data/review/ingredient_nutrition_crosswalk.jsonl",
    "data/review/recipe_time_graph_decisions.csv",
    "data/reference/ingredient_nutrition.jsonl",
    "data/reference/usda_fooddata_central_manifest.json",
)

_ARTIFACT_SCHEMA_VERSIONS = {
    "rag_documents": "2.0.0",
    "nutrition_features": "2.0.0",
    "step_tasks": "2.0.0",
    "recipe_nutrition_input_views": "2.1.0",
}


def artifact_schema_version(artifact_name: str) -> str:
    """Return the canonical schema version for a declared runtime artifact."""
    return _ARTIFACT_SCHEMA_VERSIONS.get(artifact_name, "1.0.0")


def canonical_source_manifest() -> SourceManifest:
    """批准固定源的规范 SourceManifest（单一来源）。"""
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


def canonical_build_input_manifest() -> dict:
    """绑定正式 rebuild 的全部事实输入；可再生模型缓存明确不在其中。"""
    inputs = []
    for relative_path in _BUILD_INPUT_PATHS:
        path = PROJECT_ROOT / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"构建输入不存在: {relative_path}")
        raw = path.read_bytes()
        inputs.append(
            {
                "relative_path": relative_path,
                "byte_size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return {
        "schema_version": "2.0.0",
        "fixed_recipe_source": canonical_source_manifest().model_dump(mode="json"),
        "inputs": tuple(inputs),
    }


def load_verified_recipe_source(path: Path, manifest: SourceManifest) -> list[SourceRecipeRow]:
    """校验固定源并逐行解析，返回全部 2,000 行（不在 B1 丢弃）。"""
    # 在读取任何业务行之前核验源文件事实。
    verify_source_file(path, manifest)

    raw = path.read_bytes()
    text = raw.decode(manifest.encoding)
    parsed = list(csv.reader(io.StringIO(text)))
    headers = tuple(parsed[0])
    if headers != tuple(manifest.headers):
        # verify_source_file 已核对表头，这里仅防御性再断言。
        raise RuntimeError(f"表头不一致: {headers}")

    rows: list[SourceRecipeRow] = []
    for index, row in enumerate(parsed[1:], start=1):
        if len(row) != 4:
            raise RuntimeError(f"第 {index + 1} 行列数异常: {len(row)}")
        name = row[_HEADER_INDEX["名称"]].strip()
        ingredients = row[_HEADER_INDEX["食材清单"]].strip()
        steps = row[_HEADER_INDEX["烹饪步骤"]].strip()
        labels = row[_HEADER_INDEX["label"]].strip()

        # recipe_id = row_index；禁止名称排序和变体重编号。
        row_sha256 = hashlib.sha256(
            json.dumps(
                {"名称": name, "食材清单": ingredients, "烹饪步骤": steps, "label": labels},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        rows.append(
            SourceRecipeRow(
                recipe_id=index,
                source_row_number=index,
                name=name,
                ingredients_raw=ingredients,
                steps_raw=steps,
                labels_raw=labels,
                row_sha256=row_sha256,
            )
        )

    if len(rows) != manifest.row_count:
        raise RuntimeError(f"行数不守恒: {len(rows)} != {manifest.row_count}")
    return rows
