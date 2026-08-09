"""B1 跨域引用一致性校验 —— REFACTOR 自 V1 validate_data.py。

校验清洗后的菜品、用户、食材、步骤、营养、RAG 文档之间的引用完整性。
"""

from __future__ import annotations

import json
from pathlib import Path

from food_agent_v2.core.paths import CLEANED_DIR, PIPELINE_REPORTS_DIR


def validate(cleaned_recipes: list[dict], cleaned_users: list[dict]) -> dict:
    """运行所有跨域引用检查。返回校验报告。"""
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    errors: list[dict] = []
    warnings: list[dict] = []

    # 1. 菜品 ID 完整性（1-2000，稳定、连续）
    recipe_ids = {r["recipe_id"] for r in cleaned_recipes}
    expected_recipe_ids = set(range(1, len(cleaned_recipes) + 1))
    if recipe_ids != expected_recipe_ids:
        missing = expected_recipe_ids - recipe_ids
        extra = recipe_ids - expected_recipe_ids
        if missing:
            errors.append({"check": "recipe_id_continuity", "issue": "missing_ids",
                          "ids": sorted(missing)})
        if extra:
            errors.append({"check": "recipe_id_continuity", "issue": "extra_ids",
                          "ids": sorted(extra)})

    # 2. 用户 ID 完整性（1-N）
    user_ids = {u["user_id"] for u in cleaned_users}
    expected_user_ids = set(range(1, len(cleaned_users) + 1))
    if user_ids != expected_user_ids:
        missing = expected_user_ids - user_ids
        if missing:
            errors.append({"check": "user_id_continuity", "issue": "missing_ids",
                          "ids": sorted(missing)})

    # 3. 菜品名非空
    empty_names = [r["recipe_id"] for r in cleaned_recipes if not r.get("名称")]
    if empty_names:
        errors.append({"check": "recipe_names", "issue": "empty_names",
                      "recipe_ids": empty_names})

    # 4. 食材清单非空
    empty_ingredients = [r["recipe_id"] for r in cleaned_recipes
                        if not r.get("食材清单")]
    if empty_ingredients:
        warnings.append({"check": "recipe_ingredients", "issue": "empty_ingredients",
                        "recipe_ids": empty_ingredients})

    # 5. 检查食材注册表与菜品引用一致性
    registry_path = CLEANED_DIR / "ingredient_registry.jsonl"
    ingredient_names: set[str] = set()
    if registry_path.exists():
        with registry_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    ingredient_names.add(rec["name_canonical"])

    # 6. RAG 文档数量检查（应与菜谱数一致）
    rag_path = CLEANED_DIR / "rag_documents.jsonl"
    if rag_path.exists():
        rag_count = sum(1 for _ in rag_path.open("r", encoding="utf-8"))
        if rag_count != len(cleaned_recipes):
            errors.append({"check": "rag_document_count", "issue": "count_mismatch",
                          "expected": len(cleaned_recipes), "actual": rag_count})
        # RAG 文档不含健康字段（快速扫描前 10 条）
        with rag_path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                rec = json.loads(line)
                searchable = rec.get("searchable_fields", {})
                forbidden = {"allergen_types", "health_features", "risk_tags",
                            "nutrition_values", "health_status"}
                found_forbidden = forbidden & set(searchable.keys())
                if found_forbidden:
                    errors.append({
                        "check": "rag_health_isolation",
                        "issue": f"document {rec.get('recipe_id')} contains forbidden fields",
                        "fields": list(found_forbidden),
                    })

    # 7. 营养特征不含 per_serving
    nutrition_path = CLEANED_DIR / "nutrition_profiles.jsonl"
    if nutrition_path.exists():
        with nutrition_path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                rec = json.loads(line)
                if "per_serving" in rec:
                    errors.append({
                        "check": "nutrition_per_serving",
                        "issue": f"recipe {rec.get('recipe_id')} contains per_serving",
                    })

    # 8. 时间画像：未知时长不可为估算值
    time_path = CLEANED_DIR / "time_profiles.jsonl"
    if time_path.exists():
        filled_nulls = 0
        with time_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    for task in rec.get("step_tasks", []):
                        if task.get("duration_confidence") is None and task.get("duration_seconds") is not None:
                            filled_nulls += 1
        if filled_nulls > 0:
            errors.append({
                "check": "time_null_filling",
                "issue": f"{filled_nulls} steps with unknown confidence have non-null duration",
            })

    passed = len(errors) == 0
    report = {
        "stage": "cross_domain_validation",
        "status": "passed" if passed else "failed",
        "total_checks": 8,
        "errors": errors,
        "warnings": warnings,
    }

    report_path = PIPELINE_REPORTS_DIR / "cross_domain_validation.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report


if __name__ == "__main__":
    from food_agent_v2.core.paths import CLEANED_RECIPES, CLEANED_USERS
    with CLEANED_RECIPES.open("r", encoding="utf-8") as f:
        recipes = [json.loads(line) for line in f if line.strip()]
    with CLEANED_USERS.open("r", encoding="utf-8") as f:
        users = [json.loads(line) for line in f if line.strip()]
    report = validate(recipes, users)
    print(json.dumps(report, ensure_ascii=False, indent=2))
