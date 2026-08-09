"""B1 健康关系构建器。

构建 constraint_code × ingredient_id 的审核关系表。
V2 将健康关系数据从 B4 的运行时逻辑中分离——B1 在离线阶段预计算全部关系对，
B4 在运行时只做 O(1) 查找。
"""

from __future__ import annotations

import json
from pathlib import Path

from food_agent_v2.core.paths import CLEANED_DIR, PIPELINE_REPORTS_DIR


# 食材分类关键词 → constraint_code 映射
# 每个约束代码对应一组食材名模式（基于食材名中的关键词匹配）
CONSTRAINT_INGREDIENT_PATTERNS: dict[str, list[str]] = {
    # 过敏类
    "allergy_seafood": ["虾", "蟹", "贝", "鱿鱼", "章鱼", "墨鱼", "蛤", "蚝", "蛏", "蚌",
                        "海参", "海胆", "海蜇", "螺", "鲍鱼", "扇贝", "青口", "花甲",
                        "龙虾", "基围虾", "对虾", "明虾", "皮皮虾", "小龙虾", "鳌虾",
                        "三文鱼", "金枪鱼", "鳕鱼", "鲈鱼", "鲳鱼", "带鱼", "黄鱼",
                        "鳗鱼", "鲶鱼", "石斑", "多宝鱼", "龙利鱼", "比目鱼", "鳟鱼",
                        "鱼"],
    "allergy_peanut": ["花生", "花生酱", "花生粉", "花生碎", "花生油"],
    "allergy_tree_nut": ["核桃", "杏仁", "腰果", "榛子", "松子", "开心果", "夏威夷果",
                         "巴旦木", "碧根果", "栗子", "瓜子"],
    "allergy_dairy": ["牛奶", "奶油", "黄油", "芝士", "奶酪", "炼乳", "酸奶", "乳清"],
    "allergy_egg": ["鸡蛋", "鸭蛋", "蛋黄", "蛋白", "蛋液", "鹌鹑蛋", "咸蛋", "皮蛋"],
    "allergy_soy": ["豆腐", "豆浆", "豆皮", "腐竹", "豆干", "豆腐乳", "黄豆", "毛豆"],
    "allergy_wheat": ["面粉", "面包", "面条", "馒头", "饺子", "馄饨", "包子", "吐司",
                      "蛋糕", "饼干"],
    "allergy_sesame": ["芝麻", "芝麻酱", "芝麻油", "香油"],
    # 独立虾/蟹/芒果过敏码（B2 对"虾"派生出 allergy_shrimp、"蟹/螃蟹"→allergy_crab、"芒果"→allergy_mango）
    "allergy_shrimp": ["虾", "基围虾", "对虾", "明虾", "皮皮虾", "小龙虾", "龙虾",
                       "虾仁", "虾皮", "虾米", "鲜虾", "河虾", "大头虾", "虾饺", "虾滑"],
    "allergy_crab": ["蟹", "螃蟹", "大闸蟹", "梭子蟹", "青蟹", "蟹肉", "蟹黄", "蟹粉",
                     "蟹柳", "蟹肉棒", "蟹膏", "蟹腿"],
    "allergy_mango": ["芒果", "芒果干", "芒果酱", "芒果肉", "芒果泥"],
    "allergy_fish": ["鱼", "鲈鱼", "鳕鱼", "三文鱼", "鲫鱼", "带鱼", "草鱼", "黄鱼",
                     "鳗鱼", "鲳鱼", "鲶鱼", "龙利鱼", "比目鱼", "鱼丸", "鱼片",
                     "鱼头", "鱼露", "鱼子"],
    "allergy_shellfish": ["贝", "蛤", "蚝", "蛏", "蚌", "螺", "扇贝", "青口", "花甲",
                          "牡蛎", "干贝", "鲍鱼", "海螺", "蛏子", "蛤蜊"],
    "allergy_alcohol": ["啤酒", "白酒", "红酒", "黄酒", "料酒", "米酒", "酒酿", "醪糟",
                        "葡萄酒", "朗姆酒", "威士忌", "白兰地", "绍兴酒"],

    # 疾病类
    "disease_hyperuricemia": ["动物内脏", "猪肝", "猪心", "猪腰", "猪肚", "鸡肝", "鸭肝",
                              "牛百叶", "毛肚", "黄喉", "腰花",
                              "沙丁鱼", "凤尾鱼", "鱼子", "虾", "蟹", "贝",
                              "浓汤", "高汤", "骨头汤", "火锅底料", "啤酒",
                              "香菇", "紫菜", "海带", "干贝", "瑶柱", "牡蛎"],
    "disease_diabetes": ["白糖", "白砂糖", "冰糖", "红糖", "蜂蜜", "糖浆", "果酱",
                         "蜂蜜", "麦芽糖", "甜面酱"],
    "disease_hypertension": ["咸菜", "酸菜", "泡菜", "腊肉", "腊肠", "火腿", "咸鱼",
                             "榨菜", "腌", "酱菜", "腐乳", "豆豉", "味精", "鸡精",
                             "高盐", "重盐"],
    "disease_hyperlipidemia": ["猪油", "肥肉", "奶油", "黄油", "油炸", "酥皮",
                               "动物内脏", "猪肝", "猪脑", "蟹黄", "鱼子",
                               "蛋黄", "虾膏"],

    # 特殊人群
    "group_pregnancy": ["酒精", "生食", "生鱼", "刺身", "含酒精", "酒酿",
                        "咖啡因", "咖啡", "浓茶",
                        "薏米", "薏仁", "山楂", "螃蟹", "甲鱼",
                        "未熟", "半生"],
    "group_lactation": ["酒精", "啤酒", "白酒", "红酒", "料酒", "酒酿", "醪糟",
                        "咖啡因", "咖啡", "浓茶", "辛辣", "辣椒", "麻辣",
                        "生食", "生鱼", "刺身"],
    "group_child": ["酒精", "咖啡因", "咖啡", "浓茶", "辛辣", "辣椒", "麻辣"],
    "group_elderly": ["高盐", "高糖", "高脂"],

    # 异常指标
    "indicator_high_uric_acid": ["动物内脏", "海鲜", "虾", "蟹", "贝", "啤酒", "浓汤"],
    "indicator_high_glucose": ["糖", "蜂蜜", "甜食", "高糖"],
    "indicator_high_cholesterol": ["动物内脏", "蛋黄", "奶油", "黄油", "肥肉", "虾膏",
                                   "蟹黄", "鱼子"],
    "indicator_high_bp": ["高盐", "咸", "腌", "腊", "味精", "鸡精"],
}


# 适合单字宽松子串匹配的"高精度"字符（海鲜类）：
# 经真实数据核对，含这些字的可食用食材几乎全部是真实的对应海鲜，
# 只需排除少量误判（蟹味菇/贝贝南瓜/川贝/鱼腥草）。
# 其余单字关键词（咸/辣/生/糖/腌/腊等）继续使用保守词边界匹配，避免误伤生菜、生姜、生抽等。
BROAD_SINGLE_CHARS = {"虾", "蟹", "贝", "鱼", "蛤", "蚝", "蛏", "蚌", "螺"}
_SINGLE_CHAR_GUARDS = {
    "蟹": lambda n: ("菇" in n or "菌" in n),          # 蟹味菇/海鲜菇：蘑菇不是蟹
    "贝": lambda n: ("贝贝南瓜" in n or "川贝" in n),   # 贝贝南瓜/川贝：非贝类
    "鱼": lambda n: ("腥草" in n),                      # 鱼腥草：草本非鱼
    "蚝": lambda n: ("素蚝油" in n),                    # 素蚝油：素食调味汁，不含蚝
    "虾": lambda n: False,
    "蛤": lambda n: False,
    "蛏": lambda n: False,
    "蚌": lambda n: False,
    "螺": lambda n: False,
}


def _ingredient_matches_pattern(name_canonical: str, keywords: list[str]) -> bool:
    """检查食材名是否命中任一关键词。

    多字关键词：子串匹配（关键词足够具体，误判风险低）。
    单字关键词：
      - 海鲜类字符（虾/蟹/贝/鱼）：子串匹配 + 误判守卫
        （"蟹"命中"螃蟹"、"蟹肉"，不命中"蟹味菇"；"鱼"命中"鲈鱼"、"鱼片"，不命中"鱼腥草"）
      - 其余单字字符：保守词边界匹配，仅当该字独立成词时命中
    """
    for kw in keywords:
        if len(kw) >= 2:
            if kw in name_canonical:
                return True
        else:
            if kw in BROAD_SINGLE_CHARS:
                # 海鲜单字：宽松子串匹配 + 误判守卫
                guard = _SINGLE_CHAR_GUARDS.get(kw, lambda n: False)
                if kw in name_canonical and not guard(name_canonical):
                    return True
                continue
            # 保守单字匹配：该字必须独立成词（前后为非中文字符或边界）才命中
            if _single_char_boundary_match(name_canonical, kw):
                return True

    return False


def _single_char_boundary_match(name: str, kw: str) -> bool:
    """保守单字匹配：字符独立成词（开头/结尾或前后非中文）才命中。"""
    idx = name.find(kw)
    while idx != -1:
        prev_char = name[idx - 1] if idx > 0 else ""
        next_char = name[idx + 1] if idx + 1 < len(name) else ""
        prev_ok = idx == 0 or not _is_chinese(prev_char)
        next_ok = idx + 1 >= len(name) or not _is_chinese(next_char)
        if prev_ok and next_ok:
            return True
        idx = name.find(kw, idx + 1)
    return False


def _is_chinese(ch: str) -> bool:
    """判断字符是否为中文字符（CJK统一表意文字）。"""
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or  # CJK Unified
            0x3400 <= cp <= 0x4DBF or  # CJK Extended A
            0x20000 <= cp <= 0x2A6DF)  # CJK Extended B


def build_health_relations(
    ingredient_registry: list[dict],
) -> tuple[list[dict], dict]:
    """构建全部约束—食材关系对。"""
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    relations: list[dict] = []
    stats: dict[str, dict[str, int]] = {}

    # 审核全部注册表食材（含 is_edible=false）：菜品的健康视图可能引用非可食用条目，
    # INV-018 要求覆盖"标准食材全集"，不能只审核可食用子集。
    for code, patterns in CONSTRAINT_INGREDIENT_PATTERNS.items():
        matched = 0
        for ing in ingredient_registry:
            if _ingredient_matches_pattern(ing["name_canonical"], patterns):
                relations.append({
                    "constraint_code": code,
                    "ingredient_id": ing["ingredient_id"],
                    "ingredient_name": ing["name_canonical"],
                    "review_status": "approved",
                    "hard_filter": True,
                    "evidence_ref": f"pattern_match:{code}",
                })
                matched += 1
        stats[code] = {"total_ingredients_matched": matched}

    output_path = CLEANED_DIR / "health_relations.jsonl"
    with output_path.open("w", encoding="utf-8") as f:
        for rel in relations:
            f.write(json.dumps(rel, ensure_ascii=False) + "\n")

    # INV-018：写覆盖记录——每个码针对"全部标准食材（含非可食用）"的完整审核集。
    # B4 运行时可据此校验：某码无覆盖记录或菜品的食材不在覆盖集内 → 系统失败，不是 PASS。
    all_ids = [ing["ingredient_id"] for ing in ingredient_registry]
    coverage_path = CLEANED_DIR / "health_relation_coverage.jsonl"
    with coverage_path.open("w", encoding="utf-8") as f:
        for code in CONSTRAINT_INGREDIENT_PATTERNS:
            f.write(json.dumps({
                "constraint_code": code,
                "review_status": "complete",
                "covered_ingredient_ids": all_ids,
                "evidence_ref": f"coverage:{code}:all_ingredients",
            }, ensure_ascii=False) + "\n")

    summary = {
        "stage": "health_relations",
        "total_relations": len(relations),
        "constraint_codes": len(stats),
        "coverage_codes": len(CONSTRAINT_INGREDIENT_PATTERNS),
        "covered_ingredient_count": len(all_ids),
        "by_constraint": stats,
        "output": str(output_path.relative_to(CLEANED_DIR.parent)),
        "coverage_output": str(coverage_path.relative_to(CLEANED_DIR.parent)),
        "status": "passed",
    }

    return relations, summary
