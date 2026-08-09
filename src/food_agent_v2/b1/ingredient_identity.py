"""B1 食材身份注册表构建。

从清洗后的菜品中提取所有食材名称，解析为标准食材 ID，构建食材注册表。
V2 中健康推断（allergen/risk/nutrition tags）不在此处——迁至 B4 离线关系构建。
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path

from food_agent_v2.core.paths import CLEANED_RECIPES, CLEANED_DIR, PIPELINE_REPORTS_DIR
from food_agent_v2.b1.schemas import IngredientRecord


# ---- 基础别名映射（从 V1 catalog.py 提取，仅身份解析部分） ----

# 非食用材料关键词
NON_EDIBLE_KEYWORDS = {"棉线", "荷叶", "粽叶", "竹签", "锡纸", "保鲜膜", "牙签",
                        "食品蜡纸", "食品用蜡纸", "纱布", "厨房用纸", "烘焙纸", "竹筒",
                        "竹叶", "蒸笼纸", "冰袋", "竹网", "竹垫", "一次性手套",
                        "食品级硅胶垫", "蒸布", "火腿肠衣", "肠衣", "竹制蒸笼垫",
                        "竹篮", "木炭", "果木炭", "烧烤炭"}

# 基础调料（不计入食材约束审核）
BASIC_PANTRY = {"盐", "白糖", "白砂糖", "冰糖", "红糖", "蜂蜜", "生抽", "老抽",
                "酱油", "醋", "陈醋", "白醋", "料酒", "黄酒", "蚝油", "味精",
                "鸡精", "胡椒粉", "花椒", "花椒粉", "八角", "桂皮", "香叶",
                "干辣椒", "辣椒", "生姜", "姜", "蒜", "大蒜", "葱", "小葱",
                "大葱", "洋葱", "香菜", "食用油", "花生油", "菜籽油", "香油",
                "芝麻油", "橄榄油", "猪油", "玉米油", "大豆油", "葵花籽油",
                "淀粉", "玉米淀粉", "土豆淀粉", "红薯淀粉", "生粉", "水淀粉",
                "清水", "水", "冷水", "热水", "温水", "开水", "沸水", "纯净水"}

# 数量/单位前缀模式
QTY_PATTERN = re.compile(
    r"^[（(]?\s*"
    r"(?:约|大约|适量|少许|少量|若干|足量|一大勺|一小勺|"
    r"几滴|几片|几瓣|几颗|几个|几块|几段|几根|数片|"
    r"\d+(?:\.\d+)?(?:\s*[-–—~～]\s*\d+(?:\.\d+)?)?)\s*"
    r"(?:克|g|千克|kg|公斤|毫克|mg|毫升|ml|升|l|L|"
    r"茶匙|汤匙|大匙|小匙|勺|大勺|小勺|杯|碗|"
    r"片|瓣|颗|个|块|段|根|只|条|张|把|"
    r"茶勺|汤勺|量杯|汤匙|茶匙|"
    r"打|撮|滴|束|包|盒|袋|罐|瓶|"
    r"英寸|寸|尺|英尺|厘米|cm|毫米|mm|"
    r"磅|盎司|oz|lb)?"
    r"\s*[)）]?\s*"
)


# 数量/单位后缀模式（数量在食材名后面："盐2克"、"鸡蛋1个"）
# 不要求 $ 锚定——有的食材名格式是"梨肉1000g（（切块））"
QTY_SUFFIX_PATTERN = re.compile(
    r"\s*(?:约|大约|适量|少许|少量|若干|足量)?\s*"
    r"\d+(?:\.\d+)?(?:\s*[-–—~～]\s*\d+(?:\.\d+)?)?\s*"
    r"(?:克|g|千克|kg|公斤|毫克|mg|毫升|ml|升|l|L|"
    r"茶匙|汤匙|大匙|小匙|勺|大勺|小勺|杯|碗|"
    r"片|瓣|颗|个|块|段|根|只|条|张|把|"
    r"茶勺|汤勺|量杯|汤匙|茶匙|"
    r"打|撮|滴|束|包|盒|袋|罐|瓶|"
    r"英寸|寸|尺|英尺|厘米|cm|毫米|mm|"
    r"磅|盎司|oz|lb)"
)


def normalize_ingredient_name(raw: str) -> str:
    """去除数量前缀和后缀，返回干净食材名。"""
    # 去除常见前缀（"主料："、"辅料："等）
    cleaned = re.sub(r"^[主辅调配原]料[：:]\s*", "", raw).strip()
    # 去除前缀数量
    cleaned = QTY_PATTERN.sub("", cleaned).strip()
    # 去除嵌在名称和括号说明之间的数量/单位
    # "梨肉1000g（（切块））" → "梨肉（（切块））"
    cleaned = QTY_SUFFIX_PATTERN.sub("", cleaned).strip()
    # 去除常见修饰语和括号说明
    cleaned = re.sub(r"[（(].*?[)）]", "", cleaned).strip()
    cleaned = re.sub(r"（{1,2}[^)）]*[)）]{1,2}", "", cleaned).strip()
    cleaned = cleaned.rstrip("。，,;；.!！?？").strip()
    return cleaned if cleaned else raw.strip()


def split_ingredients(ingredients_raw: str) -> list[str]:
    """将食材清单拆分为独立食材名。"""
    parts = re.split(r"[；;，,、\n]", ingredients_raw)
    return [p.strip() for p in parts if p.strip()]


def is_non_edible(name: str) -> bool:
    """判断是否为非食用材料。"""
    return name in NON_EDIBLE_KEYWORDS


def is_basic_pantry(name: str) -> bool:
    """判断是否为基础调料。"""
    return name in BASIC_PANTRY


def build_ingredient_registry(cleaned_recipes: list[dict]) -> tuple[list[dict], dict]:
    """从清洗后的菜品中提取所有食材名，构建食材注册表。

    V2 关键变更：
    - 不做过敏原/风险/营养标签推断（迁至 B4）
    - 不做健康规则（迁至 B4）
    - 只提取食材身份：名称、分类、食用性、基础调料标记
    """
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # 收集所有食材名
    name_counts: dict[str, int] = {}
    name_to_recipes: dict[str, list[int]] = {}

    for recipe in cleaned_recipes:
        rid = recipe["recipe_id"]
        raw = recipe.get("食材清单", "")
        names = split_ingredients(raw)
        for name in names:
            clean_name = normalize_ingredient_name(name)
            if clean_name:
                name_counts[clean_name] = name_counts.get(clean_name, 0) + 1
                if clean_name not in name_to_recipes:
                    name_to_recipes[clean_name] = []
                name_to_recipes[clean_name].append(rid)

    # 按频率排序，分配 stable ingredient_id
    sorted_names = sorted(name_counts.items(), key=lambda x: (-x[1], x[0]))
    registry: list[dict] = []
    registry_path = CLEANED_DIR / "ingredient_registry.jsonl"

    for idx, (name, count) in enumerate(sorted_names, start=1):
        edible = not is_non_edible(name)
        pantry = is_basic_pantry(name)
        registry.append({
            "ingredient_id": idx,
            "name_canonical": name,
            "is_edible": edible,
            "is_basic_pantry": pantry,
            "occurrence_count": count,
            "appears_in_recipes": list(OrderedDict.fromkeys(name_to_recipes[name]))[:50],
        })

    with registry_path.open("w", encoding="utf-8") as f:
        for rec in registry:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    summary = {
        "stage": "ingredient_registry",
        "unique_ingredient_names": len(registry),
        "non_edible_count": sum(1 for r in registry if not r["is_edible"]),
        "basic_pantry_count": sum(1 for r in registry if r["is_basic_pantry"]),
        "singleton_count": sum(1 for r in registry if r["occurrence_count"] == 1),
        "output": str(registry_path.relative_to(CLEANED_DIR.parent)),
        "status": "passed",
    }

    return registry, summary


if __name__ == "__main__":
    with CLEANED_RECIPES.open("r", encoding="utf-8") as f:
        recipes = [json.loads(line) for line in f if line.strip()]
    _, report = build_ingredient_registry(recipes)
    print(json.dumps(report, ensure_ascii=False, indent=2))
