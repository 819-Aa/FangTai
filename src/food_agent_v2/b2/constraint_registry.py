"""B2 封闭健康约束代码注册表（T10）。

- `ALLOWED_CONSTRAINT_CODES` 是经 B4 覆盖审核矩阵确认的封闭代码全集；
- 未知疾病/过敏/指标/生理阶段**不得动态生成代码**，映射函数对未知返回 `None`
  （fail-closed），而不是拼接 `f"xxx_{text}"`；
- 特殊阶段（含"备孕"）走显式规则表：已知但尚无批准代码的阶段（备孕）
  不产生硬约束，由服务记录为待澄清/审查信号。
"""

from __future__ import annotations

ALLOWED_CONSTRAINT_CODES = frozenset(
    {
        # 过敏
        "allergy_alcohol", "allergy_crab", "allergy_dairy", "allergy_egg",
        "allergy_fish", "allergy_mango", "allergy_peanut", "allergy_pineapple",
        "allergy_seafood", "allergy_sesame", "allergy_shellfish", "allergy_shrimp",
        "allergy_soy", "allergy_tree_nut", "allergy_wheat",
        # 疾病
        "disease_anemia", "disease_chd", "disease_diabetes", "disease_fatty_liver",
        "disease_gout", "disease_hypercholesterolemia", "disease_hyperglycemia",
        "disease_hyperlipidemia", "disease_hypertension", "disease_hyperthyroidism",
        "disease_hyperuricemia", "disease_hypothyroidism", "disease_kidney",
        "disease_obesity", "disease_osteoporosis",
        # 生理阶段
        "group_child", "group_elderly", "group_lactation", "group_pregnancy",
        # 指标
        "indicator_high_bp", "indicator_high_cholesterol", "indicator_high_glucose",
        "indicator_high_uric_acid",
    }
)

#: 已知生理阶段显式规则；值为批准代码或 None（已识别但无批准代码 → fail-closed）。
SPECIAL_STAGE_RULES: dict[str, str | None] = {
    "孕妇": "group_pregnancy",
    "哺乳期": "group_lactation",
    "儿童": "group_child",
    "老人": "group_elderly",
    "备孕": None,  # 已识别，封闭注册表暂无批准代码 → 待澄清/审查，禁止动态造码
}

_ALLERGY_MAP = {
    "花生": "allergy_peanut",
    "坚果": "allergy_tree_nut",
    "牛奶": "allergy_dairy",
    "鸡蛋": "allergy_egg",
    "海鲜": "allergy_seafood",
    "虾": "allergy_shrimp",
    "蟹": "allergy_crab",
    "螃蟹": "allergy_crab",
    "鱼": "allergy_fish",
    "贝类": "allergy_shellfish",
    "贝壳": "allergy_shellfish",
    "大豆": "allergy_soy",
    "豆制品": "allergy_soy",
    "豆类": "allergy_soy",
    "小麦": "allergy_wheat",
    "芝麻": "allergy_sesame",
    "芒果": "allergy_mango",
    "菠萝": "allergy_pineapple",
    "啤酒": "allergy_alcohol",
    "酒精": "allergy_alcohol",
}

_DISEASE_MAP = {
    "高血压": "disease_hypertension",
    "高血脂": "disease_hyperlipidemia",
    "高胆固醇": "disease_hypercholesterolemia",
    "高血糖": "disease_hyperglycemia",
    "糖尿病": "disease_diabetes",
    "高尿酸": "disease_hyperuricemia",
    "痛风": "disease_gout",
    "肾病": "disease_kidney",
    "脂肪肝": "disease_fatty_liver",
    "冠心病": "disease_chd",
    "肥胖": "disease_obesity",
    "贫血": "disease_anemia",
    "骨质疏松": "disease_osteoporosis",
    "甲亢": "disease_hyperthyroidism",
    "甲减": "disease_hypothyroidism",
}

_INDICATOR_MAP = {
    "血压": "indicator_high_bp",
    "收缩压": "indicator_high_bp",
    "舒张压": "indicator_high_bp",
    "空腹血糖": "indicator_high_glucose",
    "血糖": "indicator_high_glucose",
    "尿酸": "indicator_high_uric_acid",
    "总胆固醇": "indicator_high_cholesterol",
}

#: 特殊人群字段中出现的慢病（数据把慢病存于特殊人群）。
_SPECIAL_DISEASE_MAP = {
    "高血压": "disease_hypertension",
    "高血糖": "disease_diabetes",  # B1 仅维护 disease_diabetes 的关系
    "糖尿病": "disease_diabetes",
    "高尿酸": "disease_hyperuricemia",
    "痛风": "disease_gout",
    "高血脂": "disease_hyperlipidemia",
    "高胆固醇": "disease_hypercholesterolemia",
    "肥胖": "disease_obesity",
}


def is_allowed_code(code: str | None) -> bool:
    return code is not None and code in ALLOWED_CONSTRAINT_CODES


def allergy_to_constraint_code(name: str) -> str | None:
    """过敏名 → 批准代码；未知返回 None（不动态造码）。"""
    return _ALLERGY_MAP.get((name or "").strip())


def disease_to_constraint_code(name: str) -> str | None:
    """疾病名 → 批准代码；未知返回 None。"""
    return _DISEASE_MAP.get((name or "").strip())


def indicator_to_constraint_code(indicator: str) -> str | None:
    """归一化指标名 → 批准代码；未知返回 None。"""
    normalized = indicator.replace("_mmol/L", "").replace("_mmol/l", "").replace("_mmHg", "").replace("_umol/L", "").replace("_mg/dL", "").replace("_g/L", "").strip()
    return _INDICATOR_MAP.get(normalized)


def special_group_to_constraint_code(group: str) -> str | None:
    """特殊人群/慢病 → 批准代码；未知或已知无批准代码阶段返回 None。"""
    group = (group or "").strip()
    if group in _SPECIAL_DISEASE_MAP:
        return _SPECIAL_DISEASE_MAP[group]
    return SPECIAL_STAGE_RULES.get(group)


def special_stage_status(group: str) -> tuple[str | None, bool]:
    """返回 (代码, 是否已知阶段)。备孕等已知但无批准代码 → (None, True)。"""
    group = (group or "").strip()
    if group in _SPECIAL_DISEASE_MAP:
        return _SPECIAL_DISEASE_MAP[group], True
    if group in SPECIAL_STAGE_RULES:
        return SPECIAL_STAGE_RULES[group], True
    return None, False
