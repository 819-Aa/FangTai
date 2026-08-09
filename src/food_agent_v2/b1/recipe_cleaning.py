"""B1 菜品清洗 —— REFACTOR 自 V1 preprocess_recipes.py。

V2 变更：
- 移除步骤补写（SUPPLEMENTAL_STEPS）——未知步骤保持空字符串
- 移除标签推断（infer_retrieval_tags）——标签推断迁至 B3
- 保留：稳定 recipe_id、GBK/UTF-8 编码检测、重名变体追踪
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from food_agent_v2.core.paths import RECIPES_RAW, CLEANED_RECIPES, PROJECT_ROOT, PIPELINE_REPORTS_DIR
from food_agent_v2.b1.schemas import RecipeCleaningOutput


def _normalize_for_comparison(value: str) -> str:
    return "".join((value or "").split()).replace("：", ":").replace("；", ";")


def load_raw_recipes(path: Path) -> tuple[list[dict[str, str]], list[str], str]:
    for encoding in ("gbk", "utf-8-sig", "utf-8"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                return list(reader), list(reader.fieldnames or []), encoding
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Cannot decode CSV: {path}")


def _audit_duplicates(rows: list[dict[str, str]]) -> dict:
    """同名菜去重审计——同名不同料/不同步骤的保留为变体。"""
    by_name: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for row_number, row in enumerate(rows, start=1):
        by_name.setdefault((row.get("名称") or "").strip(), []).append((row_number, row))

    groups = []
    for name, items in by_name.items():
        if not name or len(items) < 2:
            continue
        exact_pairs = 0
        for l in range(len(items)):
            for r in range(l + 1, len(items)):
                left_sig = (
                    _normalize_for_comparison(items[l][1].get("食材清单", "")),
                    _normalize_for_comparison(items[l][1].get("烹饪步骤", "")),
                )
                right_sig = (
                    _normalize_for_comparison(items[r][1].get("食材清单", "")),
                    _normalize_for_comparison(items[r][1].get("烹饪步骤", "")),
                )
                if left_sig == right_sig:
                    exact_pairs += 1
        groups.append({
            "name": name, "count": len(items),
            "source_row_numbers": [n for n, _ in items],
            "exact_content_pairs": exact_pairs,
            "handling": "retain_as_variants",
        })
    return {
        "same_name_group_count": len(groups),
        "exact_duplicate_pair_count": sum(g["exact_content_pairs"] for g in groups),
        "groups": groups,
    }


def audit_raw(rows: list[dict[str, str]], fields: list[str]) -> dict:
    name_field = fields[0]
    return {
        "row_count": len(rows),
        "fields": fields,
        "empty_counts": {
            f: sum(not (row.get(f) or "").strip() for row in rows) for f in fields
        },
        "duplicates": _audit_duplicates(rows),
        "encoding_detected": "reported in clean summary",
    }


def clean_one(row: dict[str, str], row_number: int, variant_index: int, variant_count: int) -> RecipeCleaningOutput:
    name = (row.get("名称") or "").strip()
    ingredients = (row.get("食材清单") or "").strip()
    steps_raw = (row.get("烹饪步骤") or "").strip()
    labels_raw = (row.get("label") or "").strip()

    # V2 关键变更：不补写步骤，不推断标签。标签和步骤清洗由 B3 完成。
    steps_final = steps_raw  # 不再调用 SUPPLEMENTAL_STEPS
    labels_clean = labels_raw  # 不再调用 split_tags + infer_retrieval_tags

    notes = []
    if not steps_final:
        notes.append("steps_empty")
    if not labels_raw:
        notes.append("label_empty_original")

    return RecipeCleaningOutput(
        source_row_number=row_number,
        recipe_id=row_number,
        name=name,
        ingredients_raw=ingredients,
        steps_raw=steps_final,
        labels_raw=labels_clean,
        variant_index=variant_index,
        variant_count=variant_count,
        label_empty=not bool(labels_raw),
        steps_empty=not bool(steps_final),
        cleaning_notes=notes,
    )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def clean_recipes() -> tuple[list[dict], dict]:
    """主入口。返回 (清洗后的菜品列表, 汇总报告)。"""
    CLEANED_RECIPES.parent.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    rows, fields, encoding = load_raw_recipes(RECIPES_RAW)
    audit = audit_raw(rows, fields)

    name_counts = Counter((row.get("名称") or "").strip() for row in rows)
    name_seen: Counter[str] = Counter()
    cleaned: list[dict] = []

    for row_number, row in enumerate(rows, start=1):
        name = (row.get("名称") or "").strip()
        name_seen[name] += 1
        obj = clean_one(row, row_number, name_seen[name], name_counts[name])
        cleaned.append({
            "recipe_id": obj.recipe_id,
            "名称": obj.name,
            "食材清单": obj.ingredients_raw,
            "烹饪步骤": obj.steps_raw,
            "label": obj.labels_raw,
            "_cleaning": {
                "source_row_number": obj.source_row_number,
                "variant_index": obj.variant_index,
                "variant_count": obj.variant_count,
                "label_empty_original": obj.label_empty,
                "steps_empty_original": obj.steps_empty,
                "same_name_variant": obj.variant_count > 1,
                "steps_supplemented": False,  # V2: never supplement
                "notes": obj.cleaning_notes,
            },
        })

    _write_jsonl(CLEANED_RECIPES, cleaned)

    summary = {
        "stage": "recipe_cleaning",
        "source_encoding": encoding,
        "raw_count": len(rows),
        "clean_count": len(cleaned),
        "same_name_variant_groups": audit["duplicates"]["same_name_group_count"],
        "exact_duplicate_pairs": audit["duplicates"]["exact_duplicate_pair_count"],
        "steps_empty": sum(1 for c in cleaned if not c["烹饪步骤"]),
        "label_empty": sum(1 for c in cleaned if not c["label"]),
        "status": "passed",
    }

    return cleaned, summary


if __name__ == "__main__":
    _, report = clean_recipes()
    print(json.dumps(report, ensure_ascii=False, indent=2))
