"""B1 RAG 检索文档构建 —— REFACTOR 自 V1 build_recipe_knowledge.py。

V2 变更：
- 移除所有健康字段（allergen_types, health_features, risk_tags）
- 移除营养值
- 只保留非健康检索字段：餐次、口味、菜系、烹饪方式、菜品类型、温度、口感、场景
- 步骤摘要从步骤文本前 80 字生成
"""

from __future__ import annotations

import json
from pathlib import Path

from food_agent_v2.core.paths import CLEANED_RECIPES, CLEANED_DIR, PIPELINE_REPORTS_DIR


# V2 允许的非健康检索字段（不含 allergen/health/risk/nutrition）
ALLOWED_SEARCH_FIELDS = {"meal", "taste", "cuisine", "cooking_method", "dish_type",
                          "temperature", "texture", "occasion"}

# 菜系关键词（简化版，从 V1 catalog 提取）
CUISINE_KEYWORDS: dict[str, list[str]] = {
    "川湘菜": ["川", "湘", "麻辣", "香辣", "鱼香", "宫保", "水煮", "回锅", "剁椒"],
    "粤菜": ["粤", "清蒸", "白切", "煲", "豉汁", "叉烧", "烧腊", "虾饺"],
    "江浙菜": ["苏", "浙", "东坡", "龙井", "西湖", "糖醋", "叫花"],
    "东北菜": ["东北", "炖", "锅包", "地三鲜", "酸菜"],
    "西北风味": ["西北", "兰州", "羊肉泡", "大盘鸡", "馕"],
    "家常菜": ["家常", "下饭", "快手"],
    "海鲜风味": ["海鲜", "清蒸", "白灼"],
}


def _build_searchable_text(recipe: dict) -> str:
    """构建不含健康和营养信息的可检索文本。"""
    parts = [recipe.get("名称", "")]
    # 只保留名称本身的检索关键词
    ingredients = recipe.get("食材清单", "")
    parts.append(ingredients[:200])  # 食材前 200 字
    steps = recipe.get("烹饪步骤", "")
    if steps:
        parts.append(steps[:200])   # 步骤前 200 字
    return " ".join(parts)


def _build_step_summary(steps_raw: str) -> str | None:
    if not steps_raw:
        return None
    # 取前 80 字作为摘要
    return steps_raw[:80].strip() + ("..." if len(steps_raw) > 80 else "")


def _detect_cuisine(name: str, ingredients: str) -> str | None:
    """简单菜系检测。"""
    combined = name + " " + ingredients
    for cuisine, keywords in CUISINE_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                return cuisine
    return None


def build_rag_documents(cleaned_recipes: list[dict]) -> tuple[list[dict], dict]:
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    docs: list[dict] = []
    for recipe in cleaned_recipes:
        rid = recipe["recipe_id"]
        name = recipe["名称"]
        ingredients = recipe.get("食材清单", "")
        steps = recipe.get("烹饪步骤", "")
        labels = recipe.get("label", "")

        searchable_text = _build_searchable_text(recipe)
        step_summary = _build_step_summary(steps)
        cuisine = _detect_cuisine(name, ingredients)

        # V2: searchable_fields 只包含非健康字段
        searchable_fields = {
            "name": name,
        }
        if cuisine:
            searchable_fields["cuisine"] = cuisine
        if labels:
            searchable_fields["labels"] = labels

        docs.append({
            "recipe_id": rid,
            "document_id": f"recipe_{rid:04d}",
            "name": name,
            "searchable_text": searchable_text,
            "searchable_fields": searchable_fields,
            "step_summary": step_summary,
            "time_reference": None,  # 在 B5 构建后补充
            # V2 关键移除项（确认不存在）:
            # - allergen_types, health_features, risk_tags, nutrition_tags
            # - per_serving, nutrition_values
        })

    output_path = CLEANED_DIR / "rag_documents.jsonl"
    with output_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    summary = {
        "stage": "rag_documents",
        "total_recipes": len(cleaned_recipes),
        "total_documents": len(docs),
        "output": str(output_path.relative_to(CLEANED_DIR.parent)),
        "status": "passed",
    }

    return docs, summary


if __name__ == "__main__":
    with CLEANED_RECIPES.open("r", encoding="utf-8") as f:
        recipes = [json.loads(line) for line in f if line.strip()]
    _, report = build_rag_documents(recipes)
    print(json.dumps(report, ensure_ascii=False, indent=2))
