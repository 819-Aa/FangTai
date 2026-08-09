"""B1 营养特征构建 —— REFACTOR 自 V1 build_nutrition.py。

V2 变更：
- 移除 per_serving 和烹饪损耗计算
- 移除营养阈值和健康标签推断
- 保留：参考索引、匹配方法（精确/别名/估算）、置信度标注、数量覆盖计算
- B6 定义 NutritionProfile Schema，B1 执行离线匹配
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from food_agent_v2.b1.consumer_views import (
    IngredientIdentityFact,
    RecipeNutritionInputView,
)
from food_agent_v2.core.paths import (
    CLEANED_DIR,
    CLEANED_RECIPES,
    FOOD_COMPOSITION,
    PIPELINE_REPORTS_DIR,
)


class NutrientValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nutrient_code: str
    value: float
    unit: str
    basis: Literal["per_100g"] = "per_100g"
    source_name: str
    source_url: str


class IngredientNutritionReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient_id: int
    reference_id: str
    reference_name: str
    match_method: Literal["exact", "approved_alias"]
    nutrients: tuple[NutrientValue, ...]


class RecipeNutritionFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_id: UUID
    source_manifest_hash: str
    recipe_id: int
    available: bool
    reason: Literal["complete_reference_coverage", "missing_reference_mapping"]
    references: tuple[IngredientNutritionReference, ...]
    missing_ingredient_ids: tuple[int, ...]


def load_food_composition(path: Path) -> dict[str, dict]:
    """加载固定营养参考数据。key 为食材名（标准化后）。"""
    comp: dict[str, dict] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            name = (rec.get("name", rec.get("食物名称", rec.get("ingredient", "")))).strip()
            if name:
                comp[name] = rec
    return comp


def match_ingredient(ingredient_name: str, comp: dict[str, dict]) -> tuple[dict | None, str, str]:
    """三阶段匹配：精确 → 别名 → 未匹配。

    V2: 不进行不含数量信息的盲目估算。未匹配返回 None。
    """
    # 精确匹配
    if ingredient_name in comp:
        return comp[ingredient_name], "exact", "high"

    # 简单别名匹配（去除括号内容后重试）
    import re

    simplified = re.sub(r"[（(].*?[)）]", "", ingredient_name).strip()
    if simplified and simplified != ingredient_name and simplified in comp:
        return comp[simplified], "alias", "high"

    return None, "not_found", "low"


def index_food_composition(records: tuple[dict, ...]) -> dict[str, tuple[dict, ...]]:
    """按参考表明确名称/别名建索引；不做模糊或同类估算。"""
    indexed: dict[str, list[dict]] = {}
    for record in records:
        names = [str(record.get("name", "")).strip()]
        aliases = record.get("aliases", "")
        if isinstance(aliases, str):
            names.extend(re.split(r"[,，;；、|]", aliases))
        elif isinstance(aliases, (list, tuple)):
            names.extend(str(item) for item in aliases)
        for name in {item.strip() for item in names if item.strip()}:
            indexed.setdefault(name, []).append(record)
    return {name: tuple(values) for name, values in indexed.items()}


def build_nutrition_features_from_views(
    views: tuple[RecipeNutritionInputView, ...],
    identities: tuple[IngredientIdentityFact, ...],
    references_by_name: dict[str, tuple[dict, ...]],
) -> tuple[list[RecipeNutritionFeatures], dict]:
    """从 B6 输入视图匹配固定 per-100g 参考；缺任一映射即 unavailable。"""
    identity_by_id = {identity.ingredient_id: identity for identity in identities}
    if len(identity_by_id) != len(identities):
        raise ValueError("重复 ingredient_id")

    output: list[RecipeNutritionFeatures] = []
    for view in views:
        matched: list[IngredientNutritionReference] = []
        missing: list[int] = []
        for ingredient_id in view.ingredient_ids:
            identity = identity_by_id.get(ingredient_id)
            if identity is None:
                raise ValueError(f"未知 ingredient_id={ingredient_id}")
            candidates: list[tuple[dict, str]] = []
            for name, method in (
                (identity.name_canonical, "exact"),
                *((alias, "approved_alias") for alias in identity.aliases),
            ):
                candidates.extend((record, method) for record in references_by_name.get(name, ()))
            unique = {
                str(record.get("source_id", record.get("name", ""))): (record, method)
                for record, method in candidates
            }
            if len(unique) != 1:
                missing.append(ingredient_id)
                continue
            record, method = next(iter(unique.values()))
            reference = _build_reference(ingredient_id, record, method)
            if reference is None:
                missing.append(ingredient_id)
            else:
                matched.append(reference)

        available = not missing
        output.append(
            RecipeNutritionFeatures(
                build_id=view.build_id,
                source_manifest_hash=view.source_manifest_hash,
                recipe_id=view.recipe_id,
                available=available,
                reason=(
                    "complete_reference_coverage" if available else "missing_reference_mapping"
                ),
                references=tuple(matched),
                missing_ingredient_ids=tuple(missing),
            )
        )

    available_count = sum(item.available for item in output)
    unique_ingredient_ids = {
        ingredient_id for view in views for ingredient_id in view.ingredient_ids
    }
    mapped_ingredient_ids = {
        reference.ingredient_id for profile in output for reference in profile.references
    }
    warnings = []
    if output and available_count == 0:
        warnings.append("zero_complete_recipe_coverage")
    return output, {
        "stage": "nutrition_reference_views",
        "total_recipes": len(output),
        "available_count": available_count,
        "unavailable_count": len(output) - available_count,
        "unique_ingredient_count": len(unique_ingredient_ids),
        "mapped_unique_ingredient_count": len(mapped_ingredient_ids),
        "mapping_coverage": round(
            len(mapped_ingredient_ids) / max(len(unique_ingredient_ids), 1), 4
        ),
        "recipes_with_any_reference": sum(bool(profile.references) for profile in output),
        "warnings": warnings,
        "status": "passed",
    }


def _build_reference(
    ingredient_id: int,
    record: dict,
    match_method: str,
) -> IngredientNutritionReference | None:
    source_name = str(record.get("source_name", "")).strip()
    source_url = str(record.get("source_url", "")).strip()
    if not source_name or not source_url:
        return None
    nutrients: list[NutrientValue] = []
    for key, raw_value in sorted(record.items()):
        if not key.endswith("_per_100g") or raw_value is None or isinstance(raw_value, bool):
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        nutrient_code = key.removesuffix("_per_100g")
        unit = nutrient_code.rsplit("_", 1)[-1]
        nutrients.append(
            NutrientValue(
                nutrient_code=nutrient_code,
                value=value,
                unit=unit,
                source_name=source_name,
                source_url=source_url,
            )
        )
    if not nutrients:
        return None
    return IngredientNutritionReference(
        ingredient_id=ingredient_id,
        reference_id=str(record.get("source_id", record.get("name", ""))),
        reference_name=str(record.get("name", "")),
        match_method=match_method,
        nutrients=tuple(nutrients),
    )


def build_nutrition_features(
    cleaned_recipes: list[dict],
) -> tuple[list[dict], dict]:
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    comp = load_food_composition(FOOD_COMPOSITION)
    profiles: list[dict] = []
    matched_count = 0
    not_found_count = 0

    for recipe in cleaned_recipes:
        rid = recipe["recipe_id"]
        raw = recipe.get("食材清单", "")
        # 简单拆分（后续由 B3 做完整 split）
        import re as _re

        names = [
            _re.sub(r"^\d+[\.\、\s]*", "", n.strip()).strip()
            for n in raw.replace("；", ",").replace(";", ",").split(",")
            if n.strip()
        ]

        nutrient_values: dict[str, float] = {}
        total_matched = 0
        total_names = len([n for n in names if n])

        for name in names:
            if not name:
                continue
            ref, method, conf = match_ingredient(name, comp)
            if ref and method != "not_found":
                total_matched += 1
                # 仅保留营养素数值，不计算 per_serving
                for key in (
                    "energy_kcal",
                    "protein_g",
                    "fat_g",
                    "carb_g",
                    "sodium_mg",
                    "fiber_g",
                    "cholesterol_mg",
                ):
                    val = ref.get(key, ref.get(key.replace("_", " "), None))
                    if val is not None:
                        try:
                            nutrient_values[key] = float(val)
                        except (ValueError, TypeError):
                            pass

        coverage = total_matched / max(total_names, 1)
        confidence = "high" if coverage >= 0.8 else ("medium" if coverage >= 0.5 else "low")

        if total_matched > 0:
            matched_count += 1
        else:
            not_found_count += 1

        profiles.append(
            {
                "recipe_id": rid,
                "match_method": "exact" if coverage >= 0.8 else "partial",
                "confidence": confidence,
                "coverage_ratio": round(coverage, 4),
                "nutrient_values": nutrient_values,
                "matched_ingredients": total_matched,
                "total_ingredients": total_names,
            }
        )

    output_path = CLEANED_DIR / "nutrition_profiles.jsonl"
    with output_path.open("w", encoding="utf-8") as f:
        for p in profiles:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    summary = {
        "stage": "nutrition_features",
        "total_recipes": len(profiles),
        "matched_count": matched_count,
        "not_found_count": not_found_count,
        "match_rate": round(matched_count / max(len(profiles), 1), 4),
        "output": str(output_path.relative_to(CLEANED_DIR.parent)),
        "status": "passed",
    }

    return profiles, summary


if __name__ == "__main__":
    with CLEANED_RECIPES.open("r", encoding="utf-8") as f:
        recipes = [json.loads(line) for line in f if line.strip()]
    _, report = build_nutrition_features(recipes)
    print(json.dumps(report, ensure_ascii=False, indent=2))
