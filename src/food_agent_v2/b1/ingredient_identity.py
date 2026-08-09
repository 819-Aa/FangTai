"""B1 食材身份重建（T06 修复）。

自固定源逐行解析食材出现，构建五个产物并分离建模：
- ingredient_registry：标准身份（id、规范名、类别、食材族、冻结状态）；
- ingredient_forms：同一身份的切配形态（姜丝/姜片 → 姜，form=丝/片）；
- ingredient_aliases：审核别名（生姜→姜、大蒜→蒜），不含自映射；
- ingredient_crosswalk：merge/split/discard 决策（pending 需 H02 approve/reject）；
- recipe_ingredient_relations：recipe + occurrence -> 身份 + 角色/形态/组合语义。

身份 id 按源首次出现顺序确定性分配（R-016）。数量/单位/处理语不进入
标准名（解析层剥离）；合法数字名（T55面粉/80头干瑶柱/100%纯可可）保留。
质量门禁：qty 泄漏=0、unresolved=0、alias 唯一、类别/食材族覆盖阈值、
rejected 不合并、无幽灵身份/悬空引用。
"""

from __future__ import annotations

import csv
import json
import re
from collections import OrderedDict
from pathlib import Path

from food_agent_v2.b1.ingredient_crosswalk import (
    CrosswalkDecision,
    generate_crosswalk_decisions,
    load_approved_decisions,
    processing_base,
)
from food_agent_v2.b1.ingredient_parser import _strip_parens, _strip_quantities, parse_ingredients
from food_agent_v2.b1.schemas import SourceRecipeRow
from food_agent_v2.core.paths import CLEANED_DIR, PIPELINE_REPORTS_DIR

# ---- 遗留 API（T06 前，随 T09/T22 迁移清理）----

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

#: 类别规则（粗分类）。特定类别在前，肉类不含裸"肉"（梨肉应归水果）。
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("水果", ("梨", "苹果", "橙", "柠檬", "芒果", "草莓", "蓝莓", "桃", "葡萄",
              "西瓜", "香蕉", "菠萝", "火龙果", "猕猴桃", "石榴", "桂圆", "荔枝",
              "枣", "樱桃", "杏", "枇杷", "山楂", "柿", "果")),
    ("蔬菜", ("菜", "萝卜", "土豆", "番茄", "黄瓜", "茄子", "青椒", "辣椒", "洋葱",
              "冬瓜", "南瓜", "西葫芦", "豆角", "菠菜", "白菜", "青菜", "生菜",
              "芹菜", "茼蒿", "芦笋", "藕", "山药", "玉米", "胡萝卜", "丝瓜", "苦瓜",
              "秋葵", "百合", "木耳", "笋", "豆芽", "苋菜", "韭菜")),
    ("菌菇", ("香菇", "蘑菇", "杏鲍菇", "金针菇", "茶树菇", "猴头菇", "蟹味菇", "口蘑", "菇")),
    ("坚果", ("花生", "核桃", "杏仁", "芝麻", "腰果", "松子", "瓜子", "开心果", "栗子", "榛子")),
    ("蛋奶", ("鸡蛋", "蛋", "牛奶", "奶油", "芝士", "奶酪", "黄油", "酸奶", "奶粉")),
    ("豆制品", ("豆腐", "豆干", "腐竹", "豆浆", "香干", "千张", "豆皮")),
    ("谷物", ("米", "面", "面粉", "小麦", "糯米", "玉米面", "燕麦", "红豆", "绿豆", "薏米", "粉", "饼")),
    ("水产", ("鱼", "虾", "蟹", "蛤", "蛏", "蚝", "鲍", "鱿鱼", "墨鱼", "扇贝", "贝",
              "海参", "带鱼", "鲈鱼", "鳕鱼", "三文鱼", "牡蛎", "蟹肉", "鱼丸", "螺")),
    ("肉禽", ("猪肉", "猪", "牛", "羊", "鸡", "鸭", "鹅", "排骨", "腊肉", "火腿",
              "培根", "香肠", "内脏", "肝", "肚", "蹄", "骨", "五花肉", "里脊", "肉末", "肉丝", "鸡胸", "鸡腿", "鸡翅", "鸡爪", "肉丸")),
    ("调料", ("酱油", "生抽", "老抽", "蚝油", "醋", "料酒", "盐", "糖", "冰糖", "蜂蜜",
               "胡椒粉", "花椒", "八角", "桂皮", "香叶", "蒜", "姜", "葱", "香菜",
               "油", "淀粉", "生粉", "酱", "汁", "味精", "鸡精", "香油", "芝麻油")),
]

#: 食材族规则（细分类）。小龙虾/基围虾/对虾 → 虾族；低/中/高筋面粉 → 小麦粉族。
_FAMILY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("调味品族", ("酱油", "生抽", "老抽", "蚝油", "醋", "料酒", "黄酒", "盐", "糖",
                 "冰糖", "红糖", "蜂蜜", "味精", "鸡精", "胡椒粉", "花椒", "八角",
                 "桂皮", "香叶", "香油", "芝麻油", "橄榄油", "食用油", "花生油",
                 "菜籽油", "猪油", "玉米油", "大豆油", "葵花籽油", "淀粉", "生粉",
                 "豆瓣酱", "辣酱", "沙茶酱", "叉烧酱", "番茄酱", "烧烤酱", "韩式辣酱")),
    ("小麦粉族", ("面粉", "低筋", "中筋", "高筋")),
    ("虾族", ("虾",)),
    ("猪肉族", ("猪肉", "五花肉", "排骨", "里脊", "肉末", "肉丝", "肉片", "肉丁",
                "培根", "火腿", "香肠", "腊肉", "叉烧", "卤肉", "扣肉", "烧肉", "肉丸", "肉糜")),
    ("鸡鸭鹅族", ("鸡肉", "鸡胸", "鸡腿", "鸡翅", "鸡爪", "鸡胗", "鸡块", "鸭肉", "鸭腿", "鹅肉", "鸡", "鸭", "鹅", "鸽子", "鹌鹑")),
    ("牛羊族", ("牛肉", "牛腩", "牛腱", "羊肉", "羊排", "牛", "羊")),
    ("鱼族", ("鱼", "鲈", "鳕", "三文", "带鱼", "黄鱼", "鲫鱼", "鲳鱼", "鳗", "鳜", "鲟", "鲷")),
    ("蟹族", ("蟹",)),
    ("贝族", ("蛤", "蛏", "蚝", "鲍", "扇贝", "牡蛎", "海蛎", "贝")),
    ("蛋奶族", ("鸡蛋", "牛奶", "奶油", "奶酪", "芝士", "黄油", "酸奶", "奶粉", "炼乳", "蛋")),
    ("稻米杂粮族", ("米", "米饭", "糯米", "大米", "糙米", "紫米", "小米", "燕麦", "藜麦", "红豆", "绿豆", "薏米", "玉米面")),
    ("豆制品族", ("豆腐", "豆浆", "腐竹", "香干", "千张", "豆干", "豆皮")),
    ("菌菇族", ("菇", "蘑菇", "香菇", "杏鲍菇", "金针菇", "木耳", "口蘑", "茶树菇", "蟹味菇")),
    ("葱族", ("葱",)),
    ("姜族", ("姜",)),
    ("蒜族", ("蒜",)),
    ("根茎菜族", ("萝卜", "土豆", "山药", "红薯", "芋", "藕", "胡萝卜", "竹笋", "芦笋", "荸荠")),
    ("瓜茄菜族", ("番茄", "茄子", "黄瓜", "南瓜", "冬瓜", "丝瓜", "苦瓜", "西葫芦", "瓠瓜")),
    ("叶菜族", ("白菜", "生菜", "菠菜", "青菜", "芹菜", "茼蒿", "油麦菜", "芥蓝", "菜心", "娃娃菜", "香菜", "韭菜", "苋菜", "空心菜")),
    ("辣椒族", ("辣椒", "青椒", "彩椒", "尖椒", "泡椒", "红椒")),
    ("水果族", ("苹果", "梨", "橙", "柠檬", "芒果", "草莓", "蓝莓", "桃", "葡萄", "西瓜", "香蕉", "菠萝", "火龙果", "猕猴桃", "石榴", "桂圆", "荔枝", "枣", "樱桃", "杏", "枇杷", "山楂", "柿")),
    ("坚果籽族", ("花生", "核桃", "杏仁", "芝麻", "腰果", "松子", "瓜子", "开心果", "栗子", "核桃仁", "榛子")),
]

#: 审核同义词（源 -> 规范名）。仅当两者都在候选名中时生成候选。
_SYNONYMS: dict[str, str] = {
    "生姜": "姜",
    "大蒜": "蒜",
    "白砂糖": "白糖",
    "砂糖": "白糖",
    "小葱": "葱",
    "香葱": "葱",
    "生粉": "淀粉",
    "香油": "芝麻油",
    "陈醋": "醋",
    "白醋": "醋",
    "小米椒": "辣椒",
    "干辣椒段": "干辣椒",
}

#: 合法数字名（数量/单位解析后仍保留数字的整名白名单）。
_LEGIT_DIGIT_NAMES = {"100%纯可可", "80头干瑶柱", "NFC100%椰子水", "T45面粉", "T55面粉", "T65面粉"}

#: 类别/食材族覆盖阈值（家族经兜底后应为全量；类别阈值低于实际覆盖即门禁通过）。
_CATEGORY_COVERAGE_THRESHOLD = 0.70
_FAMILY_COVERAGE_THRESHOLD = 0.90


class IngredientIdentityError(Exception):
    """身份重建违约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def enforce_quality_gates(gates: dict, *, registry_size: int) -> None:
    """硬门禁：任何质量违约即抛 IngredientIdentityError（requirement G）。

    覆盖阈值只在规模化注册表（>=20 项）下强制执行，避免小样本单元测试误报。
    """
    if gates["qty_leakage_count"]:
        raise IngredientIdentityError(
            "QTY_UNIT_LEAKAGE", f"数量/单位泄漏 {gates['qty_leakage_count']} 项: {gates['qty_leakage_names']}"
        )
    if gates["unresolved_count"]:
        raise IngredientIdentityError("INGREDIENT_UNRESOLVED", f"未解析出现 {gates['unresolved_count']} 项")
    if not gates["alias_unique"]:
        raise IngredientIdentityError("ALIAS_NOT_UNIQUE", "别名存在多目标映射")
    if registry_size >= 20:
        if gates["category_coverage"] < gates["category_threshold"]:
            raise IngredientIdentityError(
                "CATEGORY_COVERAGE_BELOW_THRESHOLD",
                f"类别覆盖 {gates['category_coverage']} < {gates['category_threshold']}",
            )
        if gates["family_coverage"] < gates["family_threshold"]:
            raise IngredientIdentityError(
                "FAMILY_COVERAGE_BELOW_THRESHOLD",
                f"食材族覆盖 {gates['family_coverage']} < {gates['family_threshold']}",
            )
    if not gates["rejected_not_merged"]:
        raise IngredientIdentityError("REJECTED_CANDIDATE_MERGED", "被拒绝的候选仍被合并")
    if gates["dangling_reference_count"]:
        raise IngredientIdentityError("DANGLING_REFERENCE", f"悬空引用 {gates['dangling_reference_count']} 项")


def _classify(name: str) -> str:
    for category, keywords in _CATEGORY_RULES:
        if any(keyword in name for keyword in keywords):
            return category
    return "其他"


def _assign_family(name: str) -> str:
    """分配食材族；未命中具体族的按粗类别兜底，保证 family_id 不为空（issue 1）。"""
    for family, keywords in _FAMILY_RULES:
        if any(keyword in name for keyword in keywords):
            return family
    return f"{_classify(name)}通用"


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
                    "form": occ.form,
                    "is_note": occ.is_note,
                }
            )
    return occurrences


def _occurrence_evidence(occurrences: list[dict]) -> dict[str, dict]:
    """按 name_clean 汇总证据：出现次数、样例 recipe_id、原始片段。"""
    evidence: dict[str, dict] = {}
    for occ in occurrences:
        name = occ["name_clean"]
        if not name or occ["is_note"]:
            continue
        entry = evidence.setdefault(
            name,
            {"occurrence_count": 0, "sample_recipe_ids": [], "sample_fragments": []},
        )
        entry["occurrence_count"] += 1
        if len(entry["sample_recipe_ids"]) < 5 and occ["recipe_id"] not in entry["sample_recipe_ids"]:
            entry["sample_recipe_ids"].append(occ["recipe_id"])
        if len(entry["sample_fragments"]) < 3 and occ["source_fragment"] not in entry["sample_fragments"]:
            entry["sample_fragments"].append(occ["source_fragment"])
    return evidence


def _quality_leakage(names: list[str]) -> list[str]:
    return sorted(n for n in names if any(c.isdigit() for c in n) and n not in _LEGIT_DIGIT_NAMES)


def rebuild_ingredient_identities(
    rows: list[SourceRecipeRow],
    overrides_path: Path,
    staging_dir: Path,
) -> dict:
    """重建食材身份并写出产物到 staging_dir。返回报告与门禁结果。"""
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

    evidence = _occurrence_evidence(occurrences)
    id_by_name = {name: idx for idx, name in enumerate(canonical_names, start=1)}

    non_edible_names = {name for name in canonical_names if name in NON_EDIBLE_KEYWORDS}
    decisions = generate_crosswalk_decisions(
        canonical_names,
        non_edible=non_edible_names,
        synonyms=_SYNONYMS,
        occurrence_evidence=evidence,
    )

    signed = load_approved_decisions(overrides_path)
    applied: dict[str, CrosswalkDecision] = {}
    for decision in decisions:
        applied[decision.source_key] = signed.get(decision.source_key, decision)

    resolved: dict[str, int] = {}
    excluded: set[str] = set()
    rejected: set[str] = set()
    for source_key, decision in applied.items():
        if decision.review_status == "rejected":
            rejected.add(source_key)
            continue
        if decision.review_status != "approved":
            continue
        if decision.operation == "discard":
            excluded.add(source_key)
        elif decision.operation in ("merge", "split") and decision.target_ingredient_ids:
            resolved[source_key] = decision.target_ingredient_ids[0]

    merged_keys = {key for key, target in resolved.items() if target != id_by_name.get(key)}

    # 形态：被批准 form-merge 的源 -> (目标身份, 形态)。
    form_merges: dict[str, tuple[int, str]] = {}
    for source_key, decision in applied.items():
        if decision.review_status == "approved" and decision.operation == "merge" and decision.form:
            if source_key in merged_keys:
                form_merges[source_key] = (resolved[source_key], decision.form)

    # 注册表。
    registry = []
    for name in canonical_names:
        if name in excluded or name in merged_keys:
            continue
        registry.append(
            {
                "ingredient_id": id_by_name[name],
                "name_canonical": name,
                "category": _classify(name),
                "family_id": None,
                "family_name": _assign_family(name),
                "review_status": "pending",
                "occurrence_count": evidence.get(name, {}).get("occurrence_count", 0),
                "appears_in_recipes": sorted({o["recipe_id"] for o in occurrences if o["name_clean"] == name}),
            }
        )

    # 食材族注册表。
    family_names = sorted({r["family_name"] for r in registry if r["family_name"]})
    family_id_by_name = {fn: idx for idx, fn in enumerate(family_names, start=1)}
    families = [
        {"family_id": family_id_by_name[fn], "family_name": fn,
         "member_names": [r["name_canonical"] for r in registry if r["family_name"] == fn]}
        for fn in family_names
    ]

    # 关联 family_id。
    for entry in registry:
        entry["family_id"] = family_id_by_name.get(entry["family_name"])

    # 出现与关系。
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
        form = occ.get("form")
        if name in form_merges:
            # form-merge：源词条已解析到目标身份，形态为被批准形态。
            form = form_merges[name][1]
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
                "form": form,
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
                "form": form,
            }
        )

    # 别名：仅真实别名（synonym/merge 源 -> 目标），不含自映射。
    ingredient_aliases = []
    for source_key, target_id in resolved.items():
        if source_key in merged_keys and target_id != id_by_name.get(source_key):
            ingredient_aliases.append({"alias": source_key, "ingredient_id": target_id})

    # 形态：approved form-merge 的 (目标身份, 形态)。
    ingredient_forms = [
        {"ingredient_id": fid, "name_canonical": next((r["name_canonical"] for r in registry if r["ingredient_id"] == fid), ""), "form": form}
        for source, (fid, form) in form_merges.items()
    ]

    # 冻结状态。
    pending = [d.source_key for d in applied.values() if d.review_status == "pending"]
    for entry in registry:
        entry["review_status"] = "approved" if not pending else "pending"

    # 写出产物。
    for artifact_name, records in [
        ("ingredient_registry", registry),
        ("ingredient_families", families),
        ("ingredient_forms", ingredient_forms),
        ("ingredient_aliases", ingredient_aliases),
        ("ingredient_crosswalk", [d.model_dump() for d in applied.values()]),
        ("ingredient_occurrences", ingredient_occurrences),
        ("recipe_ingredient_relations", recipe_ingredient_relations),
    ]:
        path = staging / f"{artifact_name}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---- 质量门禁 ----
    leakage = _quality_leakage(canonical_names)
    alias_targets = [a["ingredient_id"] for a in ingredient_aliases]
    alias_unique = len(alias_targets) == len(set(alias_targets))
    category_coverage = sum(1 for r in registry if r["category"] != "其他") / len(registry) if registry else 0
    family_coverage = sum(1 for r in registry if r["family_name"]) / len(registry) if registry else 0
    registry_ids = {r["ingredient_id"] for r in registry}
    dangling = [rel["ingredient_id"] for rel in recipe_ingredient_relations if rel["ingredient_id"] not in registry_ids]

    gates = {
        "qty_leakage_count": len(leakage),
        "qty_leakage_names": leakage,
        "unresolved_count": len(unresolved),
        "alias_unique": alias_unique,
        "category_coverage": round(category_coverage, 4),
        "family_coverage": round(family_coverage, 4),
        "category_threshold": _CATEGORY_COVERAGE_THRESHOLD,
        "family_threshold": _FAMILY_COVERAGE_THRESHOLD,
        "rejected_not_merged": not any(k in rejected for k in merged_keys),
        "dangling_reference_count": len(dangling),
    }

    # 硬门禁：质量违约即抛错。
    enforce_quality_gates(gates, registry_size=len(registry))

    op_distribution = {
        op: sum(1 for d in applied.values() if d.operation == op)
        for op in ("merge", "split", "discard")
    }
    status_distribution = {
        st: sum(1 for d in applied.values() if d.review_status == st)
        for st in ("pending", "approved", "rejected")
    }

    # 写出 H02 富候选报告。
    h02_path = write_h02_candidates(staging, applied, id_by_name, evidence)

    return {
        "row_count": len(rows),
        "occurrence_count": len(ingredient_occurrences),
        "registry_count": len(registry),
        "family_count": len(families),
        "form_count": len(ingredient_forms),
        "alias_count": len(ingredient_aliases),
        "unresolved_count": len(unresolved),
        "qty_leakage_count": len(leakage),
        "category_coverage": round(category_coverage, 4),
        "family_coverage": round(family_coverage, 4),
        "pending_decision_count": len(pending),
        "pending_source_keys": pending,
        "operation_distribution": op_distribution,
        "status_distribution": status_distribution,
        "gates": gates,
        "h02_candidates_path": str(h02_path),
        "status": "blocked" if (pending or unresolved or leakage) else "passed",
    }


_H02_COLUMNS = [
    "source_key",
    "operation",
    "suggested_target_ingredient_id",
    "suggested_target_name",
    "target_ingredient_ids",
    "reason_code",
    "occurrence_count",
    "sample_recipe_ids",
    "sample_fragments",
    "review_status",
    "reviewer",
    "reviewed_at",
]


def write_h02_candidates(
    staging: Path,
    applied: dict[str, CrosswalkDecision],
    id_by_name: dict[str, int],
    evidence: dict[str, dict],
) -> Path:
    """写 H02 候选清单（含目标/理由/次数/recipe_id/原始片段证据），供所有者 approve/reject。"""
    rows: list[dict] = []
    for source_key in sorted(applied):
        decision = applied[source_key]
        suggested_target_id = ""
        suggested_target_name = ""
        if decision.operation == "merge" and not decision.target_ingredient_ids:
            base = processing_base(source_key)
            target_name = base if (base and base in id_by_name) else _SYNONYMS.get(source_key)
            if target_name and target_name in id_by_name:
                suggested_target_id = str(id_by_name[target_name])
                suggested_target_name = target_name
        ev = evidence.get(source_key, {})
        rows.append(
            {
                "source_key": source_key,
                "operation": decision.operation,
                "suggested_target_ingredient_id": suggested_target_id,
                "suggested_target_name": suggested_target_name,
                "target_ingredient_ids": ";".join(str(x) for x in decision.target_ingredient_ids),
                "reason_code": decision.reason_code,
                "occurrence_count": ev.get("occurrence_count", 0),
                "sample_recipe_ids": ";".join(str(x) for x in ev.get("sample_recipe_ids", [])),
                "sample_fragments": " | ".join(ev.get("sample_fragments", [])),
                "review_status": decision.review_status,
                "reviewer": decision.reviewer or "",
                "reviewed_at": decision.reviewed_at or "",
            }
        )
    path = staging / "h02_candidates.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_H02_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _normalize_old_name(name: str) -> str:
    """旧注册表名归一化：去 A料 前缀/数量/括号，用于旧→新 diff。"""
    cleaned = re.sub(r"^(?:主料|辅料|配料|A料|B料|C料|D料)[：:]\s*", "", name).strip()
    cleaned = _strip_parens(cleaned)
    return _strip_quantities(cleaned)


def build_old_to_new_diff(
    old_records: list[dict],
    new_registry: list[dict],
    id_by_name: dict[str, int],
) -> tuple[list[dict], dict]:
    """旧 3,326 身份到新身份的 merge/split/discard 差异 crosswalk。"""
    new_names = set(id_by_name)
    diff: list[dict] = []
    stats = {"identity": 0, "merge": 0, "orphan": 0, "split": 0}
    for old in old_records:
        old_name = old["name_canonical"]
        clean = _normalize_old_name(old_name)
        if old_name in new_names or clean in new_names:
            target = id_by_name.get(old_name) or id_by_name.get(clean)
            op = "identity" if old_name in new_names else "merge"
        else:
            base = processing_base(clean)
            if base and base in new_names:
                target = id_by_name[base]
                op = "merge"
            else:
                target = None
                op = "orphan"
        stats[op] = stats.get(op, 0) + 1
        diff.append(
            {
                "old_ingredient_id": old["ingredient_id"],
                "old_name": old_name,
                "operation": op,
                "new_ingredient_id": target,
                "new_name": next((n for n, i in id_by_name.items() if i == target), None) if target else None,
            }
        )
    stats["new_total"] = len(new_registry)
    stats["old_total"] = len(old_records)
    return diff, stats
