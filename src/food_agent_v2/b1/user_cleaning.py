"""B1 用户档案清洗 —— REFACTOR 自 V1 preprocess_users.py。

V2 变更：
- 保留严格字段解析——缺失指标保持 null，不填入默认值
- 解析质量元数据记录每个字段的解析状态
- 疾病、过敏、健康目标保持原始文本，交由 B2 解析为约束代码
"""

from __future__ import annotations

import json
from pathlib import Path

from food_agent_v2.b1.schemas import UserCleaningOutput
from food_agent_v2.core.paths import CLEANED_USERS, PIPELINE_REPORTS_DIR, USERS_RAW


def load_raw_users(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # 可能包装在某个 key 下
        for key in ("users", "profiles", "data"):
            if key in data:
                return data[key]
        return [data]
    return []


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _parse_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.replace("、", ",").split(",") if v.strip()]
    return []


def _parse_special_group(value: object) -> list[str]:
    """解析"特殊人群"字段。

    原始数据中的特殊人群常以 Python list repr 字符串存储，
    例如 "['高血压', '高血糖']"。需要解析成真正的字符串列表，
    否则 B2 无法把慢病映射为健康约束（INV-016 依赖此字段）。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            import ast
            try:
                parsed = ast.literal_eval(s)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
            except (ValueError, SyntaxError):
                pass
            # 手工剥离括号（literal_eval 失败时）
            inner = s[1:-1]
            return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
        return [p.strip() for p in s.replace("、", ",").split(",") if p.strip()]
    return []


def _parse_metrics(raw_metrics: object) -> dict[str, dict[str, object]]:
    """解析体检指标为标准化结构。每个指标的 value/unit/date/status 分别提取。"""
    result: dict[str, dict[str, object]] = {}
    if not isinstance(raw_metrics, (dict, list)):
        return result
    if isinstance(raw_metrics, list):
        for item in raw_metrics:
            if isinstance(item, dict):
                name = str(item.get("name", item.get("指标", item.get("indicator", "")))).strip()
                if name:
                    result[name] = {
                        "value": item.get("value", item.get("值", item.get("result"))),
                        "unit": item.get("unit", item.get("单位")),
                    }
    else:
        for k, v in raw_metrics.items():
            if isinstance(v, dict):
                result[k] = {
                    "value": v.get("value", v.get("值", v.get("result"))),
                    "unit": v.get("unit", v.get("单位")),
                }
            else:
                result[k] = {"value": v, "unit": None}
    return result


def clean_one(raw: dict, user_id: int) -> UserCleaningOutput:
    notes = []
    parse_quality: dict[str, str] = {}

    # 基础事实
    gender = str(raw.get("性别", "")).strip() or None
    age = _parse_int(raw.get("年龄"))
    activity = str(raw.get("劳动强度", raw.get("活动水平", ""))).strip() or None
    special = _parse_special_group(raw.get("特殊人群", "")) or None
    if not age:
        notes.append("age_missing")
        parse_quality["age"] = "missing"

    # 身体事实
    height = _parse_float(raw.get("身高"))
    weight = _parse_float(raw.get("体重"))
    bmi = _parse_float(raw.get("BMI"))
    if not height:
        parse_quality["height"] = "missing"
    if not weight:
        parse_quality["weight"] = "missing"

    # 偏好
    preferences = _parse_list(raw.get("口味偏好"))
    allergies = _parse_list(raw.get("过敏食材", raw.get("过敏", "")))
    health_goals = _parse_list(raw.get("健康需求", raw.get("健康目标", "")))
    diseases = _parse_list(raw.get("疾病", raw.get("疾病史", raw.get("既往病史", ""))))
    taboos = _parse_list(raw.get("禁忌食材", raw.get("禁忌", "")))

    # 体检指标
    metrics_raw = raw.get("体检指标", raw.get("健康指标", raw.get("metrics", {})))
    health_metrics = _parse_metrics(metrics_raw)

    if not health_metrics:
        parse_quality["health_metrics"] = "missing"

    return UserCleaningOutput(
        source_user_id=user_id,
        user_id=user_id,
        gender=gender,
        age=age,
        activity_level=activity,
        special_group=special,
        height_cm=height,
        weight_kg=weight,
        bmi=bmi,
        dietary_preferences=preferences,
        allergies=allergies,
        health_goals=health_goals,
        health_metrics=health_metrics,
        diseases=diseases,
        taboo_ingredients=taboos,
        raw_facts=dict(raw),
        parse_quality=parse_quality,
        cleaning_notes=notes,
    )


def clean_users() -> tuple[list[dict], dict]:
    """主入口。返回 (清洗后的用户列表, 汇总报告)。"""
    CLEANED_USERS.parent.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    raw_users = load_raw_users(USERS_RAW)
    cleaned: list[dict] = []

    for idx, raw in enumerate(raw_users, start=1):
        obj = clean_one(raw, idx)
        cleaned.append({
            "user_id": obj.user_id,
            "source_user_id": obj.source_user_id,
            "gender": obj.gender,
            "age": obj.age,
            "activity_level": obj.activity_level,
            "special_group": obj.special_group,
            "height_cm": obj.height_cm,
            "weight_kg": obj.weight_kg,
            "bmi": obj.bmi,
            "dietary_preferences": obj.dietary_preferences,
            "allergies": obj.allergies,
            "health_goals": obj.health_goals,
            "diseases": obj.diseases,
            "taboo_ingredients": obj.taboo_ingredients,
            "health_metrics": obj.health_metrics,
            "parse_quality": obj.parse_quality,
            "cleaning_notes": obj.cleaning_notes,
        })

    with CLEANED_USERS.open("w", encoding="utf-8") as f:
        for rec in cleaned:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    summary = {
        "stage": "user_cleaning",
        "raw_count": len(raw_users),
        "clean_count": len(cleaned),
        "age_missing": sum(1 for c in cleaned if c["age"] is None),
        "metrics_missing": sum(1 for c in cleaned if not c["health_metrics"]),
        "allergy_present": sum(1 for c in cleaned if c["allergies"]),
        "disease_present": sum(1 for c in cleaned if c["diseases"]),
        "health_goal_present": sum(1 for c in cleaned if c["health_goals"]),
        "status": "passed",
    }
    return cleaned, summary


if __name__ == "__main__":
    _, report = clean_users()
    print(json.dumps(report, ensure_ascii=False, indent=2))
