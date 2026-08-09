"""B1 数据库加载器 —— 将离线产物直连 MySQL 写入。"""

from __future__ import annotations

import json
from pathlib import Path

from food_agent_v2.core.config import load_config
from food_agent_v2.core.paths import CLEANED_DIR


def load_database(confirm: bool = False) -> dict:
    """将 B1 离线产物写入 MySQL。"""
    if not confirm:
        return {"status": "aborted", "reason": "run with confirm=True"}

    cfg = load_config()
    conn = _connect(cfg)
    cursor = conn.cursor()

    stages = []
    total_rows = 0

    # 1. recipes (2000)
    path = CLEANED_DIR / "clean_recipes.jsonl"
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for r in rows:
            cursor.execute(
                "INSERT INTO recipes (recipe_id, name, ingredients_raw, steps_raw, labels_raw) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name)",
                (r["recipe_id"], r["名称"], r.get("食材清单", ""),
                 r.get("烹饪步骤", ""), r.get("label", ""))
            )
        conn.commit()
        stages.append({"table": "recipes", "rows": len(rows)})
        total_rows += len(rows)

    # 2. ingredients
    path = CLEANED_DIR / "ingredient_registry.jsonl"
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for r in rows:
            cursor.execute(
                "INSERT INTO ingredients (ingredient_id, name_canonical, is_edible, is_basic_pantry) "
                "VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE name_canonical=VALUES(name_canonical)",
                (r["ingredient_id"], r["name_canonical"],
                 1 if r.get("is_edible", True) else 0,
                 1 if r.get("is_basic_pantry", False) else 0)
            )
        conn.commit()
        stages.append({"table": "ingredients", "rows": len(rows)})
        total_rows += len(rows)

    # 3. health_relations
    path = CLEANED_DIR / "health_relations.jsonl"
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for r in rows:
            cursor.execute(
                "INSERT INTO health_relations (constraint_code, ingredient_id, review_status, hard_filter, evidence_ref) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE review_status=VALUES(review_status)",
                (r["constraint_code"], r["ingredient_id"],
                 r.get("review_status", "approved"),
                 1 if r.get("hard_filter", True) else 0,
                 r.get("evidence_ref", ""))
            )
        conn.commit()
        stages.append({"table": "health_relations", "rows": len(rows)})
        total_rows += len(rows)

    # 4. nutrition_profiles
    path = CLEANED_DIR / "nutrition_profiles.jsonl"
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for r in rows:
            cursor.execute(
                "INSERT INTO nutrition_profiles (recipe_id, match_method, confidence, coverage_ratio, nutrient_values) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE confidence=VALUES(confidence)",
                (r["recipe_id"], r.get("match_method", "partial"),
                 r.get("confidence", "low"), r.get("coverage_ratio", 0),
                 json.dumps(r.get("nutrient_values", {}), ensure_ascii=False))
            )
        conn.commit()
        stages.append({"table": "nutrition_profiles", "rows": len(rows)})
        total_rows += len(rows)

    # 5. time_profiles
    path = CLEANED_DIR / "time_profiles.jsonl"
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for r in rows:
            cursor.execute(
                "INSERT INTO time_profiles (recipe_id, total_steps, total_active_seconds, "
                "total_equipment_seconds, total_passive_seconds, step_tasks) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE step_tasks=VALUES(step_tasks)",
                (r["recipe_id"], r.get("total_steps", 0),
                 r.get("total_active_seconds", 0) or 0,
                 r.get("total_equipment_seconds", 0) or 0,
                 r.get("total_passive_seconds", 0) or 0,
                 json.dumps(r.get("step_tasks", []), ensure_ascii=False))
            )
        conn.commit()
        stages.append({"table": "time_profiles", "rows": len(rows)})
        total_rows += len(rows)

    # 6. user_profiles
    path = CLEANED_DIR.parent / "cleaned" / "user_profiles.jsonl"
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for u in rows:
            cursor.execute(
                "INSERT INTO user_profiles (user_id, gender, age, dietary_preferences, "
                "allergies, health_goals, diseases, taboo_ingredients, health_metrics) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE health_metrics=VALUES(health_metrics)",
                (u["user_id"], u.get("gender") or "", u.get("age"),
                 json.dumps(u.get("dietary_preferences", []), ensure_ascii=False),
                 json.dumps(u.get("allergies", []), ensure_ascii=False),
                 json.dumps(u.get("health_goals", []), ensure_ascii=False),
                 json.dumps(u.get("diseases", []), ensure_ascii=False),
                 json.dumps(u.get("taboo_ingredients", []), ensure_ascii=False),
                 json.dumps(u.get("health_metrics", {}), ensure_ascii=False))
            )
        conn.commit()
        stages.append({"table": "user_profiles", "rows": len(rows)})
        total_rows += len(rows)

    cursor.close()
    conn.close()

    return {"status": "loaded", "total_rows": total_rows, "stages": stages}


def _connect(cfg):
    import pymysql
    return pymysql.connect(
        host=cfg.mysql.host, port=cfg.mysql.port,
        user=cfg.mysql.user, password=cfg.mysql.password,
        database=cfg.mysql.database, charset="utf8mb4",
    )
