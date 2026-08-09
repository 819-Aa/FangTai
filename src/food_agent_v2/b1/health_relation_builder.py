"""B1 健康关系构建器。

构建 constraint_code × ingredient_id 的审核关系表。
V2 将健康关系数据从 B4 的运行时逻辑中分离——B1 在离线阶段预计算全部关系对，
B4 在运行时只做 O(1) 查找。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from food_agent_v2.core.paths import CLEANED_DIR

# T10 的 B2 约束注册表必须与此闭包逐项一致；禁止运行时动态扩展。
ALLOWED_CONSTRAINT_CODES = (
    "allergy_peanut",
    "allergy_tree_nut",
    "allergy_dairy",
    "allergy_egg",
    "allergy_seafood",
    "allergy_shrimp",
    "allergy_crab",
    "allergy_fish",
    "allergy_shellfish",
    "allergy_soy",
    "allergy_wheat",
    "allergy_sesame",
    "allergy_mango",
    "allergy_pineapple",
    "allergy_alcohol",
    "disease_hypertension",
    "disease_hyperlipidemia",
    "disease_hypercholesterolemia",
    "disease_hyperglycemia",
    "disease_diabetes",
    "disease_hyperuricemia",
    "disease_gout",
    "disease_kidney",
    "disease_fatty_liver",
    "disease_chd",
    "disease_obesity",
    "disease_anemia",
    "disease_osteoporosis",
    "disease_hyperthyroidism",
    "disease_hypothyroidism",
    "indicator_high_bp",
    "indicator_high_glucose",
    "indicator_high_uric_acid",
    "indicator_high_cholesterol",
    "group_pregnancy",
    "group_lactation",
    "group_child",
    "group_elderly",
)


class HealthRelationCandidate(BaseModel):
    """机器候选只能建议，不能携带正式决定或签名。"""

    model_config = ConfigDict(extra="forbid")

    constraint_code: str
    ingredient_id: int
    ingredient_name: str
    suggested_decision: Literal["hard_exclude", "no_hard_relation"]
    suggested_reason: str
    suggested_evidence: str
    decision: None = None
    review_status: Literal["pending"] = "pending"
    reviewer: None = None
    reviewed_at: None = None


class HardHealthRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation_id: str
    constraint_code: str
    ingredient_id: int
    effect: Literal["hard_exclude"] = "hard_exclude"
    relation_basis: Literal["direct"] = "direct"
    review_status: Literal["approved"] = "approved"
    hard_filter: Literal[True] = True
    public_reason_code: str = "reviewed_health_relation"
    evidence_refs: tuple[str, ...]
    reviewer: str
    reviewed_at: str


class HealthRelationCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraint_code: str
    coverage_status: Literal["complete"] = "complete"
    universe_ingredient_count: int
    reviewed_ingredient_count: int
    relation_count: int
    evidence_ref: str


@dataclass(frozen=True)
class ApprovedHealthRelationArtifacts:
    decisions: tuple
    hard_relations: tuple[HardHealthRelation, ...]
    coverage: tuple[HealthRelationCoverage, ...]


# 食材分类关键词 → constraint_code 映射
# 每个约束代码对应一组食材名模式（基于食材名中的关键词匹配）
CONSTRAINT_INGREDIENT_PATTERNS: dict[str, list[str]] = {
    # 过敏类
    "allergy_seafood": [
        "虾",
        "蟹",
        "贝",
        "鱿鱼",
        "章鱼",
        "墨鱼",
        "蛤",
        "蚝",
        "蛏",
        "蚌",
        "海参",
        "海胆",
        "海蜇",
        "螺",
        "鲍鱼",
        "扇贝",
        "青口",
        "花甲",
        "龙虾",
        "基围虾",
        "对虾",
        "明虾",
        "皮皮虾",
        "小龙虾",
        "鳌虾",
        "三文鱼",
        "金枪鱼",
        "鳕鱼",
        "鲈鱼",
        "鲳鱼",
        "带鱼",
        "黄鱼",
        "鳗鱼",
        "鲶鱼",
        "石斑",
        "多宝鱼",
        "龙利鱼",
        "比目鱼",
        "鳟鱼",
        "鱼",
    ],
    "allergy_peanut": ["花生", "花生酱", "花生粉", "花生碎", "花生油"],
    "allergy_tree_nut": [
        "核桃",
        "杏仁",
        "腰果",
        "榛子",
        "松子",
        "开心果",
        "夏威夷果",
        "巴旦木",
        "碧根果",
        "栗子",
        "瓜子",
    ],
    "allergy_dairy": ["牛奶", "奶油", "黄油", "芝士", "奶酪", "炼乳", "酸奶", "乳清"],
    "allergy_egg": ["鸡蛋", "鸭蛋", "蛋黄", "蛋白", "蛋液", "鹌鹑蛋", "咸蛋", "皮蛋"],
    "allergy_soy": ["豆腐", "豆浆", "豆皮", "腐竹", "豆干", "豆腐乳", "黄豆", "毛豆"],
    "allergy_wheat": [
        "面粉",
        "面包",
        "面条",
        "馒头",
        "饺子",
        "馄饨",
        "包子",
        "吐司",
        "蛋糕",
        "饼干",
    ],
    "allergy_sesame": ["芝麻", "芝麻酱", "芝麻油", "香油"],
    # 独立虾/蟹/芒果过敏码（B2 对"虾"派生出 allergy_shrimp、"蟹/螃蟹"→allergy_crab、"芒果"→allergy_mango）
    "allergy_shrimp": [
        "虾",
        "基围虾",
        "对虾",
        "明虾",
        "皮皮虾",
        "小龙虾",
        "龙虾",
        "虾仁",
        "虾皮",
        "虾米",
        "鲜虾",
        "河虾",
        "大头虾",
        "虾饺",
        "虾滑",
    ],
    "allergy_crab": [
        "蟹",
        "螃蟹",
        "大闸蟹",
        "梭子蟹",
        "青蟹",
        "蟹肉",
        "蟹黄",
        "蟹粉",
        "蟹柳",
        "蟹肉棒",
        "蟹膏",
        "蟹腿",
    ],
    "allergy_mango": ["芒果", "芒果干", "芒果酱", "芒果肉", "芒果泥"],
    "allergy_fish": [
        "鱼",
        "鲈鱼",
        "鳕鱼",
        "三文鱼",
        "鲫鱼",
        "带鱼",
        "草鱼",
        "黄鱼",
        "鳗鱼",
        "鲳鱼",
        "鲶鱼",
        "龙利鱼",
        "比目鱼",
        "鱼丸",
        "鱼片",
        "鱼头",
        "鱼露",
        "鱼子",
    ],
    "allergy_shellfish": [
        "贝",
        "蛤",
        "蚝",
        "蛏",
        "蚌",
        "螺",
        "扇贝",
        "青口",
        "花甲",
        "牡蛎",
        "干贝",
        "鲍鱼",
        "海螺",
        "蛏子",
        "蛤蜊",
    ],
    "allergy_alcohol": [
        "啤酒",
        "白酒",
        "红酒",
        "黄酒",
        "料酒",
        "米酒",
        "酒酿",
        "醪糟",
        "葡萄酒",
        "朗姆酒",
        "威士忌",
        "白兰地",
        "绍兴酒",
    ],
    # 疾病类
    "disease_hyperuricemia": [
        "动物内脏",
        "猪肝",
        "猪心",
        "猪腰",
        "猪肚",
        "鸡肝",
        "鸭肝",
        "牛百叶",
        "毛肚",
        "黄喉",
        "腰花",
        "沙丁鱼",
        "凤尾鱼",
        "鱼子",
        "虾",
        "蟹",
        "贝",
        "浓汤",
        "高汤",
        "骨头汤",
        "火锅底料",
        "啤酒",
        "香菇",
        "紫菜",
        "海带",
        "干贝",
        "瑶柱",
        "牡蛎",
    ],
    "disease_diabetes": [
        "白糖",
        "白砂糖",
        "冰糖",
        "红糖",
        "蜂蜜",
        "糖浆",
        "果酱",
        "蜂蜜",
        "麦芽糖",
        "甜面酱",
    ],
    "disease_hypertension": [
        "咸菜",
        "酸菜",
        "泡菜",
        "腊肉",
        "腊肠",
        "火腿",
        "咸鱼",
        "榨菜",
        "腌",
        "酱菜",
        "腐乳",
        "豆豉",
        "味精",
        "鸡精",
        "高盐",
        "重盐",
    ],
    "disease_hyperlipidemia": [
        "猪油",
        "肥肉",
        "奶油",
        "黄油",
        "油炸",
        "酥皮",
        "动物内脏",
        "猪肝",
        "猪脑",
        "蟹黄",
        "鱼子",
        "蛋黄",
        "虾膏",
    ],
    # 特殊人群
    "group_pregnancy": [
        "酒精",
        "生食",
        "生鱼",
        "刺身",
        "含酒精",
        "酒酿",
        "咖啡因",
        "咖啡",
        "浓茶",
        "薏米",
        "薏仁",
        "山楂",
        "螃蟹",
        "甲鱼",
        "未熟",
        "半生",
    ],
    "group_lactation": [
        "酒精",
        "啤酒",
        "白酒",
        "红酒",
        "料酒",
        "酒酿",
        "醪糟",
        "咖啡因",
        "咖啡",
        "浓茶",
        "辛辣",
        "辣椒",
        "麻辣",
        "生食",
        "生鱼",
        "刺身",
    ],
    "group_child": ["酒精", "咖啡因", "咖啡", "浓茶", "辛辣", "辣椒", "麻辣"],
    "group_elderly": ["高盐", "高糖", "高脂"],
    # 异常指标
    "indicator_high_uric_acid": ["动物内脏", "海鲜", "虾", "蟹", "贝", "啤酒", "浓汤"],
    "indicator_high_glucose": ["糖", "蜂蜜", "甜食", "高糖"],
    "indicator_high_cholesterol": [
        "动物内脏",
        "蛋黄",
        "奶油",
        "黄油",
        "肥肉",
        "虾膏",
        "蟹黄",
        "鱼子",
    ],
    "indicator_high_bp": ["高盐", "咸", "腌", "腊", "味精", "鸡精"],
}

# 2026-08-10 独立审核策略校准：这里只保留能落到具体食材身份的保守候选。
# “建议限制摄入”不等于“任何用量都必须硬排除”；依赖份量、烹饪状态、疾病分期或
# 化验结果的规则不能在 B1 靠食材名称变成全局硬关系。
_ADDED_SUGAR_PATTERNS = ["白糖", "白砂糖", "冰糖", "红糖", "蜂蜜", "糖浆", "果酱", "麦芽糖"]
_HIGH_SODIUM_PATTERNS = [
    "咸菜",
    "酸菜",
    "泡菜",
    "腊肉",
    "腊肠",
    "火腿",
    "咸鱼",
    "榨菜",
    "酱菜",
    "腐乳",
    "豆豉",
    "味精",
    "鸡精",
    "高盐",
    "重盐",
]
_SATURATED_FAT_PATTERNS = ["猪油", "肥肉", "奶油", "黄油", "油炸", "酥皮", "动物内脏"]
_HIGH_PURINE_PATTERNS = [
    "动物内脏",
    "猪肝",
    "猪心",
    "猪腰",
    "鸡肝",
    "鸭肝",
    "牛百叶",
    "毛肚",
    "黄喉",
    "腰花",
    "沙丁鱼",
    "凤尾鱼",
    "鱼子",
    "虾",
    "蟹",
    "贝",
    "干贝",
    "瑶柱",
    "牡蛎",
    "啤酒",
]
_ALCOHOL_PATTERNS = [
    "酒精",
    "啤酒",
    "白酒",
    "红酒",
    "黄酒",
    "料酒",
    "米酒",
    "酒酿",
    "醪糟",
    "葡萄酒",
    "朗姆酒",
    "威士忌",
    "白兰地",
    "绍兴酒",
]

CONSTRAINT_INGREDIENT_PATTERNS.update(
    {
        "allergy_pineapple": ["菠萝", "凤梨"],
        "disease_hyperglycemia": _ADDED_SUGAR_PATTERNS,
        "disease_diabetes": _ADDED_SUGAR_PATTERNS,
        "indicator_high_glucose": _ADDED_SUGAR_PATTERNS,
        "disease_hypercholesterolemia": _SATURATED_FAT_PATTERNS,
        "disease_hyperlipidemia": _SATURATED_FAT_PATTERNS,
        "indicator_high_cholesterol": _SATURATED_FAT_PATTERNS,
        "disease_hyperuricemia": _HIGH_PURINE_PATTERNS,
        "disease_gout": _HIGH_PURINE_PATTERNS,
        "indicator_high_uric_acid": _HIGH_PURINE_PATTERNS,
        "disease_hypertension": _HIGH_SODIUM_PATTERNS,
        "indicator_high_bp": _HIGH_SODIUM_PATTERNS,
        "disease_kidney": _HIGH_SODIUM_PATTERNS,
        "disease_chd": _HIGH_SODIUM_PATTERNS + _SATURATED_FAT_PATTERNS,
        "group_pregnancy": _ALCOHOL_PATTERNS,
        "group_lactation": [],
        "group_child": [],
        "group_elderly": [],
        "disease_fatty_liver": [],
        "disease_obesity": [],
        "disease_anemia": [],
        "disease_osteoporosis": [],
        "disease_hyperthyroidism": [],
        "disease_hypothyroidism": [],
    }
)

_FDA_ALLERGEN_EVIDENCE = (
    "https://www.fda.gov/food/buy-store-serve-safe-food/food-allergies-what-you-need-know"
)
_WHO_HYPERTENSION_EVIDENCE = "https://www.who.int/news-room/fact-sheets/detail/hypertension"
_CDC_DIABETES_EVIDENCE = "https://www.cdc.gov/diabetes/healthy-eating/diabetes-meal-planning.html"
_CDC_CHOLESTEROL_EVIDENCE = "https://www.cdc.gov/cholesterol/prevention/index.html"
_NIDDK_KIDNEY_EVIDENCE = (
    "https://www.niddk.nih.gov/health-information/kidney-disease/"
    "chronic-kidney-disease-ckd/healthy-eating-adults-chronic-kidney-disease"
)
_NIDDK_FATTY_LIVER_EVIDENCE = (
    "https://www.niddk.nih.gov/health-information/liver-disease/nafld-nash/treatment"
)
_CDC_PREGNANCY_EVIDENCE = "https://www.cdc.gov/alcohol-pregnancy/about/index.html"
_CDC_LACTATION_EVIDENCE = (
    "https://www.cdc.gov/breastfeeding-special-circumstances/hcp/"
    "diet-micronutrients/maternal-diet.html"
)
_NHS_GOUT_EVIDENCE = (
    "https://www.ruh.nhs.uk/patients/services/clinical_depts/dietetics/"
    "documents/Dietary_Advice_For_Gout.pdf"
)
_GENERAL_DIET_EVIDENCE = "https://www.who.int/news-room/fact-sheets/detail/healthy-diet"

CONSTRAINT_EVIDENCE_REFS = {
    **{
        code: _FDA_ALLERGEN_EVIDENCE
        for code in ALLOWED_CONSTRAINT_CODES
        if code.startswith("allergy_")
    },
    "disease_hypertension": _WHO_HYPERTENSION_EVIDENCE,
    "indicator_high_bp": _WHO_HYPERTENSION_EVIDENCE,
    "disease_hyperglycemia": _CDC_DIABETES_EVIDENCE,
    "disease_diabetes": _CDC_DIABETES_EVIDENCE,
    "indicator_high_glucose": _CDC_DIABETES_EVIDENCE,
    "disease_hyperlipidemia": _CDC_CHOLESTEROL_EVIDENCE,
    "disease_hypercholesterolemia": _CDC_CHOLESTEROL_EVIDENCE,
    "indicator_high_cholesterol": _CDC_CHOLESTEROL_EVIDENCE,
    "disease_chd": _CDC_CHOLESTEROL_EVIDENCE,
    "disease_hyperuricemia": _NHS_GOUT_EVIDENCE,
    "disease_gout": _NHS_GOUT_EVIDENCE,
    "indicator_high_uric_acid": _NHS_GOUT_EVIDENCE,
    "disease_kidney": _NIDDK_KIDNEY_EVIDENCE,
    "disease_fatty_liver": _NIDDK_FATTY_LIVER_EVIDENCE,
    "group_pregnancy": _CDC_PREGNANCY_EVIDENCE,
    "group_lactation": _CDC_LACTATION_EVIDENCE,
    **{
        code: _GENERAL_DIET_EVIDENCE
        for code in ALLOWED_CONSTRAINT_CODES
        if code
        in {
            "disease_obesity",
            "disease_anemia",
            "disease_osteoporosis",
            "disease_hyperthyroidism",
            "disease_hypothyroidism",
            "group_child",
            "group_elderly",
        }
    },
}


# 适合单字宽松子串匹配的"高精度"字符（海鲜类）：
# 经真实数据核对，含这些字的可食用食材几乎全部是真实的对应海鲜，
# 只需排除少量误判（蟹味菇/贝贝南瓜/川贝/鱼腥草）。
# 其余单字关键词（咸/辣/生/糖/腌/腊等）继续使用保守词边界匹配，避免误伤生菜、生姜、生抽等。
BROAD_SINGLE_CHARS = {"虾", "蟹", "贝", "鱼", "蛤", "蚝", "蛏", "蚌", "螺"}
_SINGLE_CHAR_GUARDS = {
    "蟹": lambda n: "菇" in n or "菌" in n,  # 蟹味菇/海鲜菇：蘑菇不是蟹
    "贝": lambda n: "贝贝南瓜" in n or "川贝" in n,  # 贝贝南瓜/川贝：非贝类
    "鱼": lambda n: "腥草" in n,  # 鱼腥草：草本非鱼
    "蚝": lambda n: "素蚝油" in n,  # 素蚝油：素食调味汁，不含蚝
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
            if kw == "松子" and "杜松子" in name_canonical:
                continue
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
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK Unified
        or 0x3400 <= cp <= 0x4DBF  # CJK Extended A
        or 0x20000 <= cp <= 0x2A6DF
    )  # CJK Extended B


def generate_health_relation_candidates(
    allowed_constraint_codes: tuple[str, ...],
    health_ingredients: tuple[dict, ...],
) -> tuple[HealthRelationCandidate, ...]:
    """生成完整笛卡尔积候选；输出固定为 pending，绝不自动批准。"""
    if len(set(allowed_constraint_codes)) != len(allowed_constraint_codes):
        raise ValueError("allowed_constraint_codes 存在重复")
    ingredient_ids = [int(item["ingredient_id"]) for item in health_ingredients]
    if len(set(ingredient_ids)) != len(ingredient_ids):
        raise ValueError("health_ingredients 存在重复 ingredient_id")
    unknown_codes = set(allowed_constraint_codes) - set(ALLOWED_CONSTRAINT_CODES)
    if unknown_codes:
        raise ValueError(f"未知 constraint_code: {sorted(unknown_codes)}")

    candidates: list[HealthRelationCandidate] = []
    for code in allowed_constraint_codes:
        patterns = CONSTRAINT_INGREDIENT_PATTERNS.get(code, [])
        authority_ref = CONSTRAINT_EVIDENCE_REFS[code]
        for ingredient in health_ingredients:
            name = ingredient["name_canonical"]
            matched_patterns = _matching_patterns(code, name, patterns)
            if matched_patterns:
                suggestion = "hard_exclude"
                reason = "project_conservative_candidate_requires_review"
                evidence = (
                    f"authority={authority_ref};matched_patterns={'|'.join(matched_patterns)}"
                )
            else:
                suggestion = "no_hard_relation"
                reason = "no_reviewed_direct_hard_exclusion_candidate"
                evidence = f"authority={authority_ref};matched_patterns=none"
            candidates.append(
                HealthRelationCandidate(
                    constraint_code=code,
                    ingredient_id=int(ingredient["ingredient_id"]),
                    ingredient_name=name,
                    suggested_decision=suggestion,
                    suggested_reason=reason,
                    suggested_evidence=evidence,
                )
            )
    return tuple(candidates)


def build_approved_health_relation_artifacts(
    decisions: tuple,
    *,
    allowed_constraint_codes: tuple[str, ...],
    health_ingredient_ids: tuple[int, ...],
) -> ApprovedHealthRelationArtifacts:
    """把已由独立审核器验证的全矩阵冻结为关系和覆盖产物。"""
    expected = {
        (code, ingredient_id)
        for code in allowed_constraint_codes
        for ingredient_id in health_ingredient_ids
    }
    observed = {(item.constraint_code, item.ingredient_id) for item in decisions}
    if observed != expected or len(decisions) != len(expected):
        missing = sorted(expected - observed)[:10]
        extra = sorted(observed - expected)[:10]
        raise ValueError(f"批准矩阵不完整: missing={missing}, extra={extra}")

    hard_relations = tuple(
        HardHealthRelation(
            relation_id=f"health-relation:{item.constraint_code}:{item.ingredient_id}",
            constraint_code=item.constraint_code,
            ingredient_id=item.ingredient_id,
            evidence_refs=(item.evidence,),
            reviewer=item.reviewer,
            reviewed_at=item.reviewed_at,
        )
        for item in decisions
        if item.decision == "hard_exclude"
    )
    coverage = []
    for code in allowed_constraint_codes:
        code_decisions = [item for item in decisions if item.constraint_code == code]
        hard_count = sum(item.decision == "hard_exclude" for item in code_decisions)
        coverage.append(
            HealthRelationCoverage(
                constraint_code=code,
                universe_ingredient_count=len(health_ingredient_ids),
                reviewed_ingredient_count=len(code_decisions),
                relation_count=hard_count,
                evidence_ref=f"health-review-matrix:{code}",
            )
        )
    return ApprovedHealthRelationArtifacts(
        decisions=decisions,
        hard_relations=hard_relations,
        coverage=tuple(coverage),
    )


_SEAFOOD_CONDIMENT_GUARDS = ("豉油", "鼓油", "调料", "料包")
_NON_FISH_SEAFOOD_MARKERS = (
    "鲍",
    "鱿鱼",
    "章鱼",
    "墨鱼",
    "目鱼",
    "贝",
    "蛤",
    "蛏",
    "蚝",
    "蚌",
    "螺",
    "牡蛎",
    "扇贝",
    "青口",
    "花甲",
)


def _relation_name_guarded(constraint_code: str, name: str) -> bool:
    """Reject name-only matches whose wording denotes another food class or a recipe condiment."""
    if constraint_code in {"allergy_alcohol", "group_pregnancy"} and (
        "醋" in name or "无酒精" in name
    ):
        return True
    if constraint_code in {"allergy_fish", "allergy_seafood", "allergy_shrimp"} and any(
        marker in name for marker in _SEAFOOD_CONDIMENT_GUARDS
    ):
        return True
    return constraint_code == "allergy_fish" and any(
        marker in name for marker in _NON_FISH_SEAFOOD_MARKERS
    )


def _matching_patterns(code: str, name: str, patterns: list[str]) -> tuple[str, ...]:
    if _relation_name_guarded(code, name):
        return ()
    return tuple(pattern for pattern in patterns if _ingredient_matches_pattern(name, [pattern]))


def write_health_relation_candidates(
    candidates: tuple[HealthRelationCandidate, ...],
    output_path: Path,
) -> Path:
    """Write machine suggestions without turning them into approval records."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for candidate in candidates:
            payload = candidate.model_dump(mode="json")
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return output_path


def write_health_relation_candidate_csv(
    candidates: tuple[HealthRelationCandidate, ...],
    output_path: Path,
) -> Path:
    """Write a review worksheet whose decision and signature cells stay empty."""
    fieldnames = (
        "constraint_code",
        "ingredient_id",
        "decision",
        "evidence",
        "review_status",
        "reviewer",
        "reviewed_at",
        "ingredient_name",
        "suggested_decision",
        "suggested_reason",
        "suggested_evidence",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(
                {
                    "constraint_code": candidate.constraint_code,
                    "ingredient_id": candidate.ingredient_id,
                    "decision": "",
                    "evidence": "",
                    "review_status": "pending",
                    "reviewer": "",
                    "reviewed_at": "",
                    "ingredient_name": candidate.ingredient_name,
                    "suggested_decision": candidate.suggested_decision,
                    "suggested_reason": candidate.suggested_reason,
                    "suggested_evidence": candidate.suggested_evidence,
                }
            )
    return output_path


def _read_jsonl(path: Path) -> tuple[dict, ...]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


def _write_jsonl(path: Path, records: tuple[BaseModel, ...]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    return path


def run_health_relation_stage(
    *,
    ingredient_registry_path: Path,
    health_views_path: Path,
    decisions_path: Path,
    staging_dir: Path,
    builder_identity: str,
) -> dict:
    """Publish T08 candidates and freeze outputs only after H03 passes."""
    from food_agent_v2.b1.health_relation_review import (
        HealthRelationReviewError,
        load_approved_health_relation_decisions,
    )

    staging = Path(staging_dir).resolve()
    registry = _read_jsonl(ingredient_registry_path)
    registry_by_id = {int(item["ingredient_id"]): item for item in registry}
    if len(registry_by_id) != len(registry):
        raise ValueError("ingredient registry contains duplicate ingredient_id")

    health_views = _read_jsonl(health_views_path)
    health_ids = tuple(
        sorted(
            {
                int(ingredient_id)
                for view in health_views
                for ingredient_id in view["ingredient_ids"]
            }
        )
    )
    unknown_ids = set(health_ids) - set(registry_by_id)
    if unknown_ids:
        raise ValueError(
            f"health views reference unknown ingredient ids: {sorted(unknown_ids)[:10]}"
        )
    health_ingredients = tuple(registry_by_id[ingredient_id] for ingredient_id in health_ids)
    candidates = generate_health_relation_candidates(
        ALLOWED_CONSTRAINT_CODES,
        health_ingredients,
    )
    candidate_path = write_health_relation_candidates(
        candidates,
        staging / "health_relation_candidates.jsonl",
    )
    candidate_csv_path = write_health_relation_candidate_csv(
        candidates,
        staging / "health_relation_review_matrix.csv",
    )
    flagged_csv_path = write_health_relation_candidate_csv(
        tuple(item for item in candidates if item.suggested_decision == "hard_exclude"),
        staging / "health_relation_flagged_review.csv",
    )
    hard_suggestion_count = sum(item.suggested_decision == "hard_exclude" for item in candidates)
    report = {
        "stage": "T08",
        "status": "blocked",
        "approval_gate": "H03",
        "constraint_code_count": len(ALLOWED_CONSTRAINT_CODES),
        "health_ingredient_count": len(health_ids),
        "expected_decision_count": len(candidates),
        "candidate_count": len(candidates),
        "pending_candidate_count": len(candidates),
        "hard_exclude_suggestion_count": hard_suggestion_count,
        "candidate_output": str(candidate_path),
        "review_matrix_output": str(candidate_csv_path),
        "flagged_review_output": str(flagged_csv_path),
        "decisions_input": str(decisions_path.resolve()),
    }

    try:
        decisions = load_approved_health_relation_decisions(
            decisions_path,
            allowed_constraint_codes=ALLOWED_CONSTRAINT_CODES,
            health_ingredient_ids=health_ids,
            builder_identity=builder_identity,
        )
    except HealthRelationReviewError as error:
        report["blocker"] = error.code
        report["blocker_detail"] = str(error)
        (staging / "health_relation_quality_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return report

    artifacts = build_approved_health_relation_artifacts(
        decisions,
        allowed_constraint_codes=ALLOWED_CONSTRAINT_CODES,
        health_ingredient_ids=health_ids,
    )
    decisions_output = _write_jsonl(
        staging / "health_relation_decisions.jsonl", artifacts.decisions
    )
    relations_output = _write_jsonl(staging / "health_relations.jsonl", artifacts.hard_relations)
    coverage_output = _write_jsonl(
        staging / "health_relation_coverage.jsonl",
        artifacts.coverage,
    )
    report.update(
        {
            "status": "passed",
            "pending_candidate_count": 0,
            "approved_decision_count": len(artifacts.decisions),
            "hard_relation_count": len(artifacts.hard_relations),
            "decisions_output": str(decisions_output),
            "relations_output": str(relations_output),
            "coverage_output": str(coverage_output),
        }
    )
    report.pop("blocker", None)
    report.pop("blocker_detail", None)
    (staging / "health_relation_quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report


def build_health_relations(
    ingredient_registry: list[dict],
) -> tuple[list[dict], dict]:
    """Generate pending candidates for H03; never create approved relations."""
    candidates = generate_health_relation_candidates(
        ALLOWED_CONSTRAINT_CODES,
        tuple(ingredient_registry),
    )
    output_path = write_health_relation_candidates(
        candidates,
        CLEANED_DIR / "health_relation_candidates.jsonl",
    )
    hard_suggestion_count = sum(item.suggested_decision == "hard_exclude" for item in candidates)
    summary = {
        "stage": "health_relations",
        "status": "pending_review",
        "approval_gate": "H03",
        "candidate_count": len(candidates),
        "expected_decision_count": len(candidates),
        "hard_exclude_suggestion_count": hard_suggestion_count,
        "constraint_codes": len(ALLOWED_CONSTRAINT_CODES),
        "covered_ingredient_count": len(ingredient_registry),
        "output": str(output_path.relative_to(CLEANED_DIR.parent)),
    }
    return [item.model_dump(mode="json") for item in candidates], summary
