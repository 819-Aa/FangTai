"""B1 步骤时间构建 —— REFACTOR 自 V1 time_profile.py。

V2 变更：
- 不补写缺失时长——无时长步骤保持 duration_seconds=null
- 不填入固定估算值——V1 中的估算逻辑移除
- 保留：显式时长解析、步骤类型分类、设备推断
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from food_agent_v2.core.paths import CLEANED_RECIPES, CLEANED_DIR, PIPELINE_REPORTS_DIR

# 时长提取模式
TIME_PATTERNS = [
    (re.compile(r"(\d+)\s*小时"), 3600),
    (re.compile(r"(\d+)\s*分钟"), 60),
    (re.compile(r"(\d+)\s*秒"), 1),
    (re.compile(r"(\d+)\s*hour"), 3600),
    (re.compile(r"(\d+)\s*min"), 60),
]
TIME_RANGE_PATTERN = re.compile(r"(\d+)\s*[-–—~～]\s*(\d+)\s*(分钟|分)")


# 步骤类型关键词
ACTIVE_KEYWORDS = {"切", "剁", "削", "刮", "剥", "洗", "腌", "拌", "搅",
                   "揉", "擀", "包", "串", "穿", "铺", "摆", "装", "抹", "刷"}
EQUIPMENT_KEYWORDS = {"炒", "煎", "炸", "烤", "蒸", "煮", "炖", "焖", "烧",
                      "煲", "熬", "烩", "焗", "卤", "煸", "爆", "熘"}
PASSIVE_KEYWORDS = {"醒", "冷藏", "冷冻", "浸泡", "泡发", "饧", "发酵",
                    "静置", "冷却", "晾凉", "沥干", "沉淀"}


def _parse_duration(text: str) -> tuple[int | None, str | None]:
    """从步骤文本中提取全部显式时长并求和。返回 (秒, 置信度)。

    V2：不估算缺失时长；无显式时长返回 None。
    一个步骤可能含多个时长片段（如"10分钟/120℃加热；再30分钟熬煮"），求和。
    """
    total = 0
    found_high = False
    for pattern, multiplier in TIME_PATTERNS:
        for match in pattern.finditer(text):
            total += int(match.group(1)) * multiplier
            found_high = True
    range_match = TIME_RANGE_PATTERN.search(text)
    if range_match:
        low, high = int(range_match.group(1)), int(range_match.group(2))
        total += (low + high) // 2 * 60
        return total, "medium"
    if found_high:
        return total, "high"
    return None, None


def _classify_step_type(text: str) -> str:
    """根据关键词分类步骤类型。"""
    # 设备程序设置（"设置：10分钟/120℃/3档"、"选择…模式"）→ 设备占用
    if re.search(r"设置\s*[:：]|选择.{0,6}模式|模式\s*[:：]", text):
        return "equipment"
    for kw in EQUIPMENT_KEYWORDS:
        if kw in text:
            return "equipment"
    for kw in ACTIVE_KEYWORDS:
        if kw in text:
            return "active"
    for kw in PASSIVE_KEYWORDS:
        if kw in text:
            return "passive"
    return "active"  # 默认


def _infer_equipment(text: str) -> str | None:
    """推断所需设备类型。"""
    equipment_map = {
        "炒": "wok", "煎": "pan", "炸": "deep_fryer", "烤": "oven",
        "蒸": "steamer", "煮": "pot", "炖": "pot", "焖": "pot",
        "煲": "pot", "熬": "pot", "烧": "wok",
    }
    for kw, eq in equipment_map.items():
        if kw in text:
            return eq
    return None


def split_steps(steps_raw: str) -> list[str]:
    """将烹饪步骤文本拆分为独立步骤。

    支持"第X步：…；第2步：…"和数字编号格式。
    先按"第X步"切分，再按分号/句号细拆，使每个时长片段成为独立任务。
    """
    if not steps_raw:
        return []
    # 1) 按 "第X步：" 拆分
    parts = re.split(r"(?:第\s*\d+\s*步\s*[：:]\s*)", steps_raw)
    steps: list[str] = []
    for part in parts:
        # 2) 按分号/句号/换行细拆
        for sub in re.split(r"[；;。\n]+", part):
            sub = sub.strip().strip("；;。")
            if sub and len(sub) > 1:
                steps.append(sub)
    if not steps:
        return [steps_raw.strip()]
    # 去重保序（过滤重复片段）
    seen: set[str] = set()
    out: list[str] = []
    for s in steps:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def build_step_profiles(cleaned_recipes: list[dict]) -> tuple[list[dict], dict]:
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    profiles: list[dict] = []
    empty_step_count = 0
    high_conf_count = 0
    medium_conf_count = 0
    low_conf_count = 0
    total_steps = 0

    for recipe in cleaned_recipes:
        rid = recipe["recipe_id"]
        raw = recipe.get("烹饪步骤", "")
        step_texts = split_steps(raw)

        if not step_texts:
            empty_step_count += 1

        step_tasks = []
        for i, text in enumerate(step_texts):
            duration, conf = _parse_duration(text)
            step_type = _classify_step_type(text)
            equipment = _infer_equipment(text) if step_type == "equipment" else None
            # 互斥键跨菜共享：
            # - 设备占用 → 同类型设备互斥（如两个菜都用炒锅则串行）
            # - 主动操作 → 单厨师互斥（所有菜的主动操作串行）
            # - 被动等待 → 可并行（不占用厨师/设备）
            if step_type == "passive":
                mutex_key = "passive"
            elif equipment:
                mutex_key = f"equipment:{equipment}"
            else:
                mutex_key = "cook"

            # V2: 未找到显式时长 → null（不估算、不补写）
            if duration is None:
                low_conf_count += 1
                conf = None

            if conf == "high":
                high_conf_count += 1
            elif conf == "medium":
                medium_conf_count += 1

            total_steps += 1
            step_tasks.append({
                "step_index": i + 1,
                "description_raw": text,
                "step_type": step_type,
                "duration_seconds": duration,
                "duration_confidence": conf,
                "equipment_type": equipment,
                "mutex_key": mutex_key,
                "depends_on": [],  # V2: 从步骤顺序推断依赖
            })

        # 按顺序推断依赖：本菜内下一步依赖上一步（被动等待结束后才能继续本菜后续步骤）
        for i in range(1, len(step_tasks)):
            prev = step_tasks[i - 1]
            curr = step_tasks[i]
            if prev["step_index"] not in curr["depends_on"]:
                curr["depends_on"].append(prev["step_index"])

        profiles.append({
            "recipe_id": rid,
            "name": recipe["名称"],
            "step_tasks": step_tasks,
            "total_steps": len(step_tasks),
            "total_active_seconds": sum(
                s["duration_seconds"] for s in step_tasks
                if s["step_type"] == "active" and s["duration_seconds"]
            ),
            "total_equipment_seconds": sum(
                s["duration_seconds"] for s in step_tasks
                if s["step_type"] == "equipment" and s["duration_seconds"]
            ),
            "total_passive_seconds": sum(
                s["duration_seconds"] for s in step_tasks
                if s["step_type"] == "passive" and s["duration_seconds"]
            ),
        })

    output_path = CLEANED_DIR / "time_profiles.jsonl"
    with output_path.open("w", encoding="utf-8") as f:
        for p in profiles:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    summary = {
        "stage": "step_time_building",
        "total_recipes": len(profiles),
        "empty_step_count": empty_step_count,
        "total_steps": total_steps,
        "high_confidence_durations": high_conf_count,
        "medium_confidence_durations": medium_conf_count,
        "unknown_durations": low_conf_count,
        "output": str(output_path.relative_to(CLEANED_DIR.parent)),
        "status": "passed",
    }

    return profiles, summary


if __name__ == "__main__":
    with CLEANED_RECIPES.open("r", encoding="utf-8") as f:
        recipes = [json.loads(line) for line in f if line.strip()]
    _, report = build_step_profiles(recipes)
    print(json.dumps(report, ensure_ascii=False, indent=2))
