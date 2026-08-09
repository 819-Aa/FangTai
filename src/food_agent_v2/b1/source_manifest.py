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

_HEADER_INDEX = {"名称": 0, "食材清单": 1, "烹饪步骤": 2, "label": 3}


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
