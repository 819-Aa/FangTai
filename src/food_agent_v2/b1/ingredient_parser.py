"""B1 食材解析器（T06）—— 固定源食材清单的唯一解析入口。

自 b3/ingredient_parser.py 迁移并增强，覆盖黄金用例：
- 组分前缀（主料/辅料/A料/B料/C料）；
- 数量与单位（前缀/后缀，"梨肉1000g"）；
- 括号处理语（切块/压碎/浸泡/干重/约2g）——不得进入食材名；
- 可选（可选/选配/选用）与替代（A或B）；
- 组合引用（酱料见/馅料见/酱汁见）；
- 处理语后缀（丝/片/段/末/蓉/丁）在身份层识别，不在解析层进入新身份。

B3/B4/B5/B6/C1 不得重复解析原始食材字符串（INV-008/INV-023）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 组分前缀：主料/辅料/配料/A料/B料/C料/D料
GROUP_PREFIX_RE = re.compile(r"^(?:主料|辅料|配料|A料|B料|C料|D料)[：:]")

#: 分割符
SPLIT_PATTERN = re.compile(r"[；;，,、\n。]")

#: 可选标记（括号内或尾部）
OPTIONAL_MARKERS = (
    re.compile(r"[（(]可选[)）]"),
    re.compile(r"[（(]选配[)）]"),
    re.compile(r"[（(]选用[)）]"),
    re.compile(r"或可选$"),
)

#: 替代标记
ALTERNATIVE_PATTERN = re.compile(r"或(?:者)?")

#: 组合引用：酱料见（X）/馅料见（X）/酱汁见（X）等
COMPOSITION_PATTERN = re.compile(
    r"(?:酱料|馅料|酱汁|蘸料|浇汁|淋酱|料汁|卤汁)\s*见\s*[（(]\s*([^)）]+?)\s*[)）]"
)

#: 数量+单位前缀
QTY_UNIT_PATTERN = re.compile(
    r"^(?:约|大约|适量|少许|少量|若干|足量)?\s*"
    r"(?:\d+(?:\.\d+)?(?:\s*[-–—~～]\s*\d+(?:\.\d+)?)?)\s*"
    r"(?:克|g|千克|kg|公斤|毫克|mg|毫升|ml|升|l|L|"
    r"茶匙|汤匙|大匙|小匙|勺|大勺|小勺|杯|碗|"
    r"片|瓣|颗|个|块|段|根|只|条|张|把|"
    r"茶勺|汤勺|量杯|英寸|寸|尺|厘米|cm|毫米|mm)?\s*"
)

#: 数量+单位后缀（"梨肉1000g（（切块））"）
QTY_UNIT_SUFFIX_PATTERN = re.compile(
    r"\s*(?:约|大约|适量|少许|少量|若干|足量)?\s*"
    r"\d+(?:\.\d+)?(?:\s*[-–—~～]\s*\d+(?:\.\d+)?)?\s*"
    r"(?:克|g|千克|kg|公斤|毫克|mg|毫升|ml|升|l|L|"
    r"茶匙|汤匙|大匙|小匙|勺|大勺|小勺|杯|碗|"
    r"片|瓣|颗|个|块|段|根|只|条|张|把|"
    r"茶勺|汤勺|量杯|英寸|寸|尺|厘米|cm|毫米|mm)\s*$"
)

#: 括号内处理语（切块/压碎/浸泡/干重/约2g 等），不得进入食材名
_PAREN_GROUP_RE = re.compile(r"[（(][^（()）]*[)）]")

#: 非食材说明标记
NOTE_MARKERS = ("适量", "少许", "少量", "若干", "足量", "根据", "见", "备", "待用")


@dataclass
class ParsedOccurrence:
    """单个食材出现（B1 解析产物）。"""

    raw_text: str
    name_clean: str
    group: str | None = None
    quantity_raw: str | None = None
    unit_raw: str | None = None
    is_optional: bool = False
    choice_group_id: int | None = None
    alternatives: list[str] = field(default_factory=list)
    composition_ref: str | None = None
    is_note: bool = False


def _detect_group(fragment: str) -> tuple[str | None, str]:
    match = GROUP_PREFIX_RE.match(fragment)
    if match:
        group = match.group(0).rstrip("：:")
        return group, fragment[match.end():].strip()
    return None, fragment


def _check_optional(text: str) -> bool:
    return any(marker.search(text) for marker in OPTIONAL_MARKERS)


_QTY_WORDS = ("适量", "少许", "少量", "若干", "足量")


def _extract_qty_name(text: str) -> tuple[str, str | None, str | None]:
    """提取 (食材名, 数量, 单位)。优先前缀数量；再处理后缀；最后剥离量词。"""
    qty_match = QTY_UNIT_PATTERN.match(text)
    if qty_match:
        qty_raw = qty_match.group().strip()
        rest = text[qty_match.end():].strip()
        unit_match = re.search(
            r"^(克|g|千克|kg|公斤|毫克|mg|毫升|ml|升|l|L|"
            r"茶匙|汤匙|大匙|小匙|勺|大勺|小勺|杯|碗|"
            r"片|瓣|颗|个|块|段|根|只|条|张|把)",
            rest,
        )
        if unit_match:
            return rest[unit_match.end():].strip(), qty_raw, unit_match.group()
        return rest, qty_raw, None
    # 后缀数量："梨肉1000g"
    suffix_match = QTY_UNIT_SUFFIX_PATTERN.search(text)
    if suffix_match:
        qty_raw = suffix_match.group().strip()
        name = text[: suffix_match.start()].strip()
        unit_match = re.search(r"(克|g|千克|kg|毫升|ml|升|l|L|个|只|条|根|片|瓣|颗|块|段|张|把)$", qty_raw)
        return name, qty_raw, unit_match.group() if unit_match else None
    # 尾部量词："盐适量"、"葱少许"
    for qty_word in _QTY_WORDS:
        if text.endswith(qty_word) and len(text) > len(qty_word):
            return text[: -len(qty_word)].strip(), qty_word, None
    return text, None, None


def _strip_parens_processing(name: str) -> str:
    """反复去掉最内层括号及其内容（处理语/说明）。可选标记已先行判定。"""
    cleaned = name
    while True:
        match = _PAREN_GROUP_RE.search(cleaned)
        if not match:
            break
        cleaned = cleaned[: match.start()] + cleaned[match.end():]
    return cleaned.strip()


def _split_alternatives(text: str) -> list[str]:
    if "或可选" in text:
        return [text.replace("或可选", "").strip()] * 2
    if "或" not in text:
        return [text]
    return [p.strip() for p in ALTERNATIVE_PATTERN.split(text) if p.strip()]


def split_ingredient_text(ingredients_raw: str) -> list[str]:
    """将食材清单拆分为独立片段（保留组前缀由解析器处理）。"""
    if not ingredients_raw:
        return []
    fragments: list[str] = []
    for p in SPLIT_PATTERN.split(ingredients_raw):
        p = p.strip()
        if not p:
            continue
        if re.match(r"^\d+[\.\、]", p):
            continue
        if p.startswith("（") or p.startswith("("):
            continue
        fragments.append(p)
    return fragments


def parse_ingredients(ingredients_raw: str) -> list[ParsedOccurrence]:
    """把食材清单解析为结构化出现列表（唯一解析入口）。"""
    occurrences: list[ParsedOccurrence] = []
    choice_group_counter = 1

    for fragment in split_ingredient_text(ingredients_raw):
        group, body = _detect_group(fragment)

        # 组合引用：酱料见（X）→ 引用指向其身份（非纯说明，保留 composition_ref）
        composition = COMPOSITION_PATTERN.search(body)
        if composition:
            occurrences.append(
                ParsedOccurrence(
                    raw_text=fragment,
                    name_clean=composition.group(1).strip(),
                    group=group,
                    composition_ref=composition.group(0),
                )
            )
            continue

        # 非食材说明：只有量词/描述词，无具体食材
        if body in NOTE_MARKERS:
            occurrences.append(
                ParsedOccurrence(raw_text=fragment, name_clean=body, group=group, is_note=True)
            )
            continue

        is_optional = _check_optional(body)
        alternatives = _split_alternatives(body)

        if len(alternatives) > 1:
            gid = choice_group_counter
            choice_group_counter += 1
            for alt in alternatives:
                clean_name, qty, unit = _extract_qty_name(_strip_parens_processing(alt))
                occurrences.append(
                    ParsedOccurrence(
                        raw_text=alt,
                        name_clean=clean_name,
                        group=group,
                        quantity_raw=qty,
                        unit_raw=unit,
                        is_optional=is_optional,
                        choice_group_id=gid,
                        alternatives=[a for a in alternatives if a != alt],
                    )
                )
        else:
            clean_name, qty, unit = _extract_qty_name(_strip_parens_processing(body))
            occurrences.append(
                ParsedOccurrence(
                    raw_text=fragment,
                    name_clean=clean_name,
                    group=group,
                    quantity_raw=qty,
                    unit_raw=unit,
                    is_optional=is_optional,
                )
            )

    return occurrences
