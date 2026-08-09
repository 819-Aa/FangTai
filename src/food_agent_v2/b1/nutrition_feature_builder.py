"""B1 营养特征构建 —— REFACTOR 自 V1 build_nutrition.py。

V2 变更：
- 移除 per_serving 和烹饪损耗计算
- 移除营养阈值和健康标签推断
- 保留：参考索引、匹配方法（精确/别名/估算）、置信度标注、数量覆盖计算
- B6 定义 NutritionProfile Schema，B1 执行离线匹配
"""

from __future__ import annotations

import json
from pathlib import Path

from food_agent_v2.core.paths import FOOD_COMPOSITION, CLEANED_RECIPES, CLEANED_DIR, PIPELINE_REPORTS_DIR


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
        names = [_re.sub(r"^\d+[\.\、\s]*", "", n.strip()).strip()
                 for n in raw.replace("；", ",").replace(";", ",").split(",")
                 if n.strip()]

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
                for key in ("energy_kcal", "protein_g", "fat_g", "carb_g",
                           "sodium_mg", "fiber_g", "cholesterol_mg"):
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

        profiles.append({
            "recipe_id": rid,
            "match_method": "exact" if coverage >= 0.8 else "partial",
            "confidence": confidence,
            "coverage_ratio": round(coverage, 4),
            "nutrient_values": nutrient_values,
            "matched_ingredients": total_matched,
            "total_ingredients": total_names,
        })

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
