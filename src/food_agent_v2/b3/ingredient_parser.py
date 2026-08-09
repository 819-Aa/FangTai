"""B3 食材解析器 —— 可选/替代拆分。

将"食材清单"文本解析为结构化的 Occurrence 列表，
识别可选食材、替代组、复合组成引用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---- 拆分规则 ----

# 分割符："，、；; \n" 和 "。"
SPLIT_PATTERN = re.compile(r"[；;，,、\n。]")

# 可选标记
OPTIONAL_MARKERS = [
    re.compile(r"[（(]可选[)）]"),
    re.compile(r"[（(]选配[)）]"),
    re.compile(r"[（(]选用[)）]"),
    re.compile(r"或可选$"),
]

# 替代标记："A或B"、"A或者B"
ALTERNATIVE_PATTERN = re.compile(r"或(?:者)?")

# 复合组成标记："酱料见"、"馅料见"、"酱汁见"
COMPOSITION_PATTERN = re.compile(r"(?:酱料|馅料|酱汁|蘸料|浇汁|淋酱)\s*[\[（(（].*?[)）\]）]")

# 数量+单位模式（用于分离数量与食材名）
QTY_UNIT_PATTERN = re.compile(
    r"^(?:约|大约|适量|少许|少量|若干|足量)?\s*"
    r"(?:\d+(?:\.\d+)?(?:\s*[-–—~～]\s*\d+(?:\.\d+)?)?)\s*"
    r"(?:克|g|千克|kg|公斤|毫克|mg|毫升|ml|升|l|L|"
    r"茶匙|汤匙|大匙|小匙|勺|大勺|小勺|杯|碗|"
    r"片|瓣|颗|个|块|段|根|只|条|张|把|"
    r"茶勺|汤勺|量杯|英寸|寸|尺|厘米|cm|毫米|mm)?\s*"
)


@dataclass
class Occurrence:
    """单个食材出现。"""
    raw_text: str                       # 原始文本片段
    name_clean: str                     # 去数量后的食材名
    quantity_raw: str | None = None     # 数量原文
    unit_raw: str | None = None         # 单位原文
    is_optional: bool = False           # 是否可选
    choice_group_id: int | None = None  # 替代组 ID（仅替代成员有值）
    alternatives: list[str] = field(default_factory=list)  # 同组其他替代名


def split_ingredient_text(ingredients_raw: str) -> list[str]:
    """将食材清单拆分为独立片段。"""
    if not ingredients_raw:
        return []
    parts = SPLIT_PATTERN.split(ingredients_raw)
    results = []
    for p in parts:
        p = p.strip()
        # 跳过纯空格/纯标点
        if not p or len(p) <= 1:
            continue
        # 跳过步骤说明混入的句子（"1. 将..."）
        if re.match(r"^\d+[\.\、]", p):
            continue
        # 跳过纯注释
        if p.startswith("（") or p.startswith("("):
            continue
        results.append(p)
    return results


def _check_optional(text: str) -> bool:
    """检查是否为可选食材。"""
    for marker in OPTIONAL_MARKERS:
        if marker.search(text):
            return True
    return False


def _split_alternatives(text: str) -> list[str]:
    """按'或'拆分替代选项。"""
    # 先检查是否真的含"或"（排除"或可选"等）
    if "或可选" in text:
        return [text.replace("或可选", "").strip(), text.replace("或可选", "").strip()]
    # 检查纯"或"
    if "或" not in text:
        return [text]
    # 基本拆分
    parts = ALTERNATIVE_PATTERN.split(text)
    return [p.strip() for p in parts if p.strip()]


def parse_ingredients(ingredients_raw: str) -> list[Occurrence]:
    """主解析函数。返回结构化的 Occurrence 列表。"""
    fragments = split_ingredient_text(ingredients_raw)
    occurrences: list[Occurrence] = []
    choice_group_counter = 1

    for fragment in fragments:
        # 检查是否为可选食材
        is_opt = _check_optional(fragment)

        # 检查是否为替代组
        alternatives = _split_alternatives(fragment)
        if len(alternatives) > 1:
            # 替代组：每个成员都创建一个 Occurrence，共享 choice_group_id
            gid = choice_group_counter
            choice_group_counter += 1
            group_alternatives = [a for a in alternatives if a]
            for alt in group_alternatives:
                clean_name, qty, unit = _extract_qty_name(alt)
                occurrences.append(Occurrence(
                    raw_text=alt,
                    name_clean=clean_name,
                    quantity_raw=qty,
                    unit_raw=unit,
                    is_optional=is_opt,
                    choice_group_id=gid,
                    alternatives=[a for a in group_alternatives if a != alt],
                ))
        else:
            # 单食材
            clean_name, qty, unit = _extract_qty_name(fragment)
            occurrences.append(Occurrence(
                raw_text=fragment,
                name_clean=clean_name,
                quantity_raw=qty,
                unit_raw=unit,
                is_optional=is_opt,
            ))

    return occurrences


def _extract_qty_name(text: str) -> tuple[str, str | None, str | None]:
    """从文本中提取 (食材名, 数量, 单位)。"""
    qty_match = QTY_UNIT_PATTERN.match(text)
    if qty_match:
        qty_raw = qty_match.group().strip()
        rest = text[qty_match.end():].strip()
        # 尝试分离单位和数值
        unit_match = re.search(
            r"^(克|g|千克|kg|公斤|毫克|mg|毫升|ml|升|l|L|"
            r"茶匙|汤匙|大匙|小匙|勺|大勺|小勺|杯|碗|"
            r"片|瓣|颗|个|块|段|根|只|条|张|把)",
            rest
        )
        if unit_match:
            unit_raw = unit_match.group()
            name_clean = rest[unit_match.end():].strip()
        else:
            unit_raw = None
            name_clean = rest
        return name_clean, qty_raw, unit_raw
    return text, None, None
