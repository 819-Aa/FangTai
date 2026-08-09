"""B1 食材身份（T06 重建 + 遗留 API 兼容）。

T06 新增 rebuild_ingredient_identities：自固定源逐行解析食材出现，构建五个产物
（occurrences/registry/aliases/crosswalk/relations），身份 id 按源首次出现
顺序确定性分配（R-016）。crosswalk merge/split/discard 决策 pending 需 H02
人工批准后才应用，无模型自动批准。

遗留 build_ingredient_registry / normalize_ingredient_name 仍保留供旧管线
（rebuild.py）与 b3.identity_resolver 使用，随 T09/T22 迁移清理。
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path

from food_agent_v2.b1.ingredient_crosswalk import (
    CrosswalkDecision,
    generate_crosswalk_decisions,
    load_approved_decisions,
)
from food_agent_v2.b1.ingredient_parser import parse_ingredients
from food_agent_v2.b1.schemas import SourceRecipeRow
from food_agent_v2.core.paths import CLEANED_DIR, PIPELINE_REPORTS_DIR

# ---- 遗留 API（T06 前）----

NON_EDIBLE_KEYWORDS = {"棉线", "荷叶", "粽叶", "竹签", "锡纸", "保鲜膜", "牙签",
                        "食品蜡纸", "食品用蜡纸", "纱布", "厨房用纸", "烘焙纸", "竹筒",
                        "竹叶", "蒸笼纸", "冰袋", "竹网", "竹垫", "一次性手套",
                        "食品级硅胶垫", "蒸布", "火腿肠衣", "肠衣", "竹制蒸笼垫",
                        "竹篮", "木炭", "果木炭", "烧烤炭"}

BASIC_PANTRY = {"盐", "白糖", "白砂糖", "冰糖", "红糖", "蜂蜜", "生抽", "老抽",
                "酱油", "醋", "陈醋", "白醋", "料酒", "黄酒", "蚝油", "味精",
                "鸡精", "胡椒粉", "花椒", "花椒粉", "八角", "桂皮", "香叶",
                "干辣椒", "辣椒", "生姜", "姜", "蒜", "大蒜", "葱", "小葱",
                "大葱", "洋葱", "香菜", "食用油", "花生油", "菜籽油", "香油",
                "芝麻油", "橄榄油", "猪油", "玉米油", "大豆油", "葵花籽油",
                "淀粉", "玉米淀粉", "土豆淀粉", "红薯淀粉", "生粉", "水淀粉",
                "清水", "水", "冷水", "热水", "温水", "开水", "沸水", "纯净水"}

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
    """遗留 API：去除数量前缀/后缀与括号说明，返回干净食材名。"""
    cleaned = re.sub(r"^[主辅调配原]料[：:]\s*", "", raw).strip()
    cleaned = QTY_PATTERN.sub("", cleaned).strip()
    cleaned = QTY_SUFFIX_PATTERN.sub("", cleaned).strip()
    cleaned = re.sub(r"[（(].*?[)）]", "", cleaned).strip()
    cleaned = re.sub(r"（{1,2}[^)）]*[)）]{1,2}", "", cleaned).strip()
    cleaned = cleaned.rstrip("。，,;；.!！?？").strip()
    return cleaned if cleaned else raw.strip()


def split_ingredients(ingredients_raw: str) -> list[str]:
    """遗留 API：将食材清单拆分为独立食材名。"""
    parts = re.split(r"[；;，,、\n]", ingredients_raw)
    return [p.strip() for p in parts if p.strip()]


def is_non_edible(name: str) -> bool:
    return name in NON_EDIBLE_KEYWORDS


def is_basic_pantry(name: str) -> bool:
    return name in BASIC_PANTRY


def build_ingredient_registry(cleaned_recipes: list[dict]) -> tuple[list[dict], dict]:
    """遗留 API：从清洗后的菜品中提取食材名，构建注册表（旧频率排序身份）。"""
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    name_counts: dict[str, int] = {}
    name_to_recipes: dict[str, list[int]] = {}
    for recipe in cleaned_recipes:
        rid = recipe["recipe_id"]
        for name in split_ingredients(recipe.get("食材清单", "")):
            clean_name = normalize_ingredient_name(name)
            if clean_name:
                name_counts[clean_name] = name_counts.get(clean_name, 0) + 1
                name_to_recipes.setdefault(clean_name, []).append(rid)

    sorted_names = sorted(name_counts.items(), key=lambda x: (-x[1], x[0]))
    registry: list[dict] = []
    registry_path = CLEANED_DIR / "ingredient_registry.jsonl"
    for idx, (name, count) in enumerate(sorted_names, start=1):
        registry.append({
            "ingredient_id": idx,
            "name_canonical": name,
            "is_edible": not is_non_edible(name),
            "is_basic_pantry": is_basic_pantry(name),
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


# ---- T06 重建 ----

#: 基础类别启发式（粗分类，供覆盖报告；精确类别由审核清单确认）。
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("调料", ("酱油", "生抽", "老抽", "蚝油", "醋", "料酒", "盐", "糖", "冰糖", "蜂蜜",
               "胡椒粉", "花椒", "八角", "桂皮", "香叶", "辣椒", "蒜", "姜", "葱", "香菜",
               "油", "淀粉", "生粉", "酱", "汁", "味精", "鸡精", "香油", "芝麻油")),
    ("肉类", ("猪肉", "猪", "牛", "羊", "鸡", "鸭", "鹅", "肉", "排骨", "腊肉", "火腿",
              "培根", "香肠", "内脏", "肝", "肚", "蹄", "骨")),
    ("水产", ("鱼", "虾", "蟹", "蛤", "蛏", "蚝", "鲍", "鱿鱼", "墨鱼", "扇贝", "贝",
              "海参", "带鱼", "鲈鱼", "鳕鱼", "三文鱼", "牡蛎", "蟹肉", "鱼丸")),
    ("蔬菜", ("菜", "萝卜", "土豆", "番茄", "黄瓜", "茄子", "青椒", "辣椒", "洋葱", "冬瓜",
              "南瓜", "西葫芦", "豆角", "菠菜", "白菜", "青菜", "生菜", "芹菜", "茼蒿",
              "芦笋", "藕", "山药", "玉米", "胡萝卜", "丝瓜", "苦瓜", "秋葵", "百合", "木耳")),
    ("水果", ("果", "苹果", "梨", "香蕉", "橙", "柠檬", "芒果", "草莓", "蓝莓", "桃",
              "葡萄", "西瓜", "火龙果", "猕猴桃", "菠萝", "石榴", "桂圆", "荔枝", "枣")),
    ("蛋奶", ("鸡蛋", "蛋", "牛奶", "奶油", "芝士", "奶酪", "黄油", "酸奶", "奶粉")),
    ("谷物", ("米", "面", "面粉", "小麦", "糯米", "玉米面", "燕麦", "红豆", "绿豆", "薏米",
              "豆沙", "年糕", "馒头", "面包", "饺子", "包子", "粉", "饼")),
    ("豆制品", ("豆腐", "豆干", "腐竹", "豆浆", "香干", "千张")),
    ("菌菇", ("香菇", "蘑菇", "杏鲍菇", "金针菇", "茶树菇", "猴头菇", "蟹味菇", "口蘑")),
    ("坚果", ("花生", "核桃", "杏仁", "芝麻", "腰果", "松子", "瓜子", "开心果", "栗子")),
]


def _classify(name: str) -> str:
    for category, keywords in _CATEGORY_RULES:
        if any(keyword in name for keyword in keywords):
            return category
    return "其他"


class IngredientIdentityError(Exception):
    """身份重建违约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _parse_recipes(rows: list[SourceRecipeRow]) -> list[dict]:
    occurrences: list[dict] = []
    for row in rows:
        for index, occ in enumerate(parse_ingredients(row.ingredients_raw), start=1):
            occurrences.append(
                {
                    "recipe_id": row.recipe_id,
                    "occurrence_index": index,
                    "source_fragment": occ.raw_text,
                    "name_clean": occ.name_clean,
                    "group": occ.group,
                    "quantity_raw": occ.quantity_raw,
                    "unit_raw": occ.unit_raw,
                    "is_optional": occ.is_optional,
                    "choice_group_id": occ.choice_group_id,
                    "alternatives": occ.alternatives,
                    "composition_ref": occ.composition_ref,
                    "is_note": occ.is_note,
                }
            )
    return occurrences


def rebuild_ingredient_identities(
    rows: list[SourceRecipeRow],
    overrides_path: Path,
    staging_dir: Path,
) -> dict:
    """重建食材身份并写出五个产物到 staging_dir。返回报告。"""
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)

    occurrences = _parse_recipes(rows)

    canonical_names: list[str] = []
    seen: set[str] = set()
    for occ in occurrences:
        name = occ["name_clean"]
        if not name or occ["is_note"] or name in seen:
            continue
        seen.add(name)
        canonical_names.append(name)

    id_by_name = {name: idx for idx, name in enumerate(canonical_names, start=1)}
    registry = [
        {
            "ingredient_id": id_by_name[name],
            "name_canonical": name,
            "category": _classify(name),
            "family_id": None,
            "aliases": [],
            "review_status": "approved",
            "occurrence_count": sum(1 for o in occurrences if o["name_clean"] == name),
            "appears_in_recipes": sorted({o["recipe_id"] for o in occurrences if o["name_clean"] == name}),
        }
        for name in canonical_names
    ]

    non_edible_names = {name for name in canonical_names if name in NON_EDIBLE_KEYWORDS}
    decisions = generate_crosswalk_decisions(
        canonical_names,
        non_edible=non_edible_names,
    )

    approved = load_approved_decisions(overrides_path)
    applied: dict[str, CrosswalkDecision] = {}
    for decision in decisions:
        applied[decision.source_key] = approved.get(decision.source_key, decision)

    resolved: dict[str, int] = {}
    excluded: set[str] = set()
    for source_key, decision in applied.items():
        if decision.review_status != "approved":
            continue
        if decision.operation == "discard":
            excluded.add(source_key)
        elif decision.operation in ("merge", "split") and decision.target_ingredient_ids:
            resolved[source_key] = decision.target_ingredient_ids[0]

    # merge/split 源词条不再作为独立身份（避免幽灵注册表条目/重复别名）。
    merged_keys = {key for key, target in resolved.items() if target != id_by_name.get(key)}

    ingredient_occurrences: list[dict] = []
    recipe_ingredient_relations: list[dict] = []
    unresolved: list[str] = []
    for occ in occurrences:
        name = occ["name_clean"]
        if not name or occ["is_note"]:
            continue
        if name in excluded:
            continue
        target_id = resolved.get(name, id_by_name.get(name))
        if target_id is None:
            unresolved.append(name)
            continue
        occurrence_id = f"{occ['recipe_id']}-{occ['occurrence_index']}"
        ingredient_occurrences.append(
            {
                "occurrence_id": occurrence_id,
                "recipe_id": occ["recipe_id"],
                "source_fragment": occ["source_fragment"],
                "name_clean": name,
                "group": occ["group"],
                "quantity_raw": occ["quantity_raw"],
                "unit_raw": occ["unit_raw"],
                "is_optional": occ["is_optional"],
                "choice_group_id": occ["choice_group_id"],
                "alternatives": occ["alternatives"],
                "composition_ref": occ["composition_ref"],
                "resolved_ingredient_id": target_id,
            }
        )
        recipe_ingredient_relations.append(
            {
                "recipe_id": occ["recipe_id"],
                "occurrence_id": occurrence_id,
                "ingredient_id": target_id,
                "role": "required" if not occ["is_optional"] else "optional",
                "choice_group_id": occ["choice_group_id"],
                "is_alternative": occ["choice_group_id"] is not None,
                "is_composition_ref": occ["composition_ref"] is not None,
            }
        )

    # 别名精确唯一：被 merge 的源词条只保留一条"源 -> 目标身份"映射，
    # 不再同时保留"源 -> 自身 id"。
    ingredient_aliases = [
        {"alias": name, "ingredient_id": target_id}
        for name, target_id in id_by_name.items()
        if name not in excluded and name not in merged_keys
    ]
    for source_key, target_id in resolved.items():
        if source_key in merged_keys:
            ingredient_aliases.append({"alias": source_key, "ingredient_id": target_id})

    # merge/split/discard 源词条都不再作为独立身份。
    registry = [r for r in registry if r["name_canonical"] not in excluded and r["name_canonical"] not in merged_keys]

    # 身份冻结前 review_status=pending；H02 全部批准后 approved。
    pending = [d.source_key for d in applied.values() if d.review_status == "pending"]
    for entry in registry:
        entry["review_status"] = "approved" if not pending else "pending"

    for artifact_name, records in [
        ("ingredient_occurrences", ingredient_occurrences),
        ("ingredient_registry", registry),
        ("ingredient_aliases", ingredient_aliases),
        ("ingredient_crosswalk", [d.model_dump() for d in applied.values()]),
        ("recipe_ingredient_relations", recipe_ingredient_relations),
    ]:
        path = staging / f"{artifact_name}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    category_coverage = sum(1 for r in registry if r["category"] != "其他")

    return {
        "row_count": len(rows),
        "occurrence_count": len(ingredient_occurrences),
        "registry_count": len(registry),
        "unresolved_count": len(unresolved),
        "unresolved_names": unresolved[:20],
        "pending_decision_count": len(pending),
        "pending_source_keys": pending,
        "category_coverage": category_coverage,
        "registry_total": len(registry),
        "status": "blocked" if pending or unresolved else "passed",
    }
