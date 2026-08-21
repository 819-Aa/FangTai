"""B1 食材解析器（T06 修复）—— 固定源食材清单的唯一解析入口。

自 b3/ingredient_parser.py 迁移并增强，覆盖黄金用例与数量/单位解析：
- 组分前缀（主料/辅料/A料/B料/C料）；
- 数量与单位：分数（1/2t）、英文单位（mL/t/T/tsp/g/kg/oz/lb）、中文计数/
  包装单位（片/个/粒/盒/罐/袋/包/瓶/根/只/朵…）、重复单位（"2片片"）、
  前缀/中缀/后缀位置；只剥离"数字+单位"，保留 T55面粉 等合法数字名；
- 括号处理语（切块/压碎/浸泡/干重/约2g）——不得进入食材名；
- 可选（可选/选配/选用）与替代（A或B）；
- 组合引用（酱料见/馅料见/酱汁见）；
- 切配形态（丝/片/末/丁/蓉/块/粒/段/花/碎）作为 form 属性记录，
  是否合并由 crosswalk/H02 决定，解析层不自动合并。

B3/B4/B5/B6/C1 不得重复解析原始食材字符串（INV-008/INV-023）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 组分前缀：主料/辅料/配料/A料/B料/C料/D料
GROUP_PREFIX_RE = re.compile(r"^(?:主料|辅料|配料|A料|B料|C料|D料)[：:]")

#: 分割符；仅在括号外生效，括号中的温度/处理说明属于同一个食材。
_SPLIT_CHARS = frozenset("；;，,、\n。")

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

#: 数量数字：整数/小数/分数
_NUMBER = r"\d+(?:[./]\d+)*(?:\.\d+)?"
#: 度量单位（重量/体积/勺子/英文）——前缀位置也剥离。
_MEASURE_UNIT = (
    r"(?:克|千克|公斤|毫克|g|kg|mg|毫升|ml|mL|升|l|L|"
    r"磅|盎司|oz|lb|斤|两|"
    r"茶匙|汤匙|大匙|小匙|勺|大勺|小勺|茶勺|汤勺|量杯|t|T|tsp|Tbsp|h)"
)
#: 计数/包装/时间/温度单位——仅后缀位置剥离（避免误删 80头/1号 等产品级名）。
_COUNT_UNIT = (
    r"(?:人份|小块|小片|小粒|小撮|小把|小个|"
    r"杯|碗|片|瓣|颗|个|块|段|根|只|条|张|把|朵|粒|枚|支|截|"
    r"盒|罐|袋|包|瓶|捆|扎|打|撮|滴|束|对|副|"
    r"盏|节|方|枝|棵|份|套|串|米|头|"
    r"英寸|寸|尺|厘米|cm|毫米|mm|"
    r"小时|分钟|秒|度|℃)"
)
_UNIT = rf"(?:{_MEASURE_UNIT}|{_COUNT_UNIT})"

#: 前缀度量数量："200g鸡胸肉"（只剥度量单位，不剥 80头 等计数前缀）。
_QTY_PREFIX = re.compile(
    rf"^(?:约|大约|适量|少许|少量|若干|足量)?\s*{_NUMBER}\s*{_MEASURE_UNIT}\s*"
)
#: 后缀数量（可重复单位 "2片片"）："红泡椒50g" / "米2人份" / "姜2片片"。
_QTY_SUFFIX = re.compile(
    rf"(?:约|大约|适量|少许|少量|若干|足量)?\s*{_NUMBER}\s*{_UNIT}(?:\s*{_UNIT})?\s*$"
)
#: 中文数字数量后缀："菠萝半个" / "葱两根" / "胡萝卜小半根"。
_CHINESE_NUMBER = r"(?:小?半|[一二两三四五六七八九十百]+|数|數|几|幾)"
_CHINESE_QTY_SUFFIX = re.compile(rf"{_CHINESE_NUMBER}\s*{_UNIT}\s*$")
#: 范围+单位后缀："鲜活蛤蜊10~15个"
_RANGE_WITH_UNIT = re.compile(rf"{_NUMBER}\s*[~\-—]\s*{_NUMBER}\s*{_UNIT}\s*$")
#: 范围后缀："土豆400~" / "冰糖5-" / "糖15—"
_RANGE_SUFFIX = re.compile(rf"{_NUMBER}\s*[~\-—]\s*\d*\s*$")
#: 定性数量+计数单位："新鲜香菇若干只"。
_QUALITATIVE_QTY_SUFFIX = re.compile(
    rf"(?:约|大约)?\s*(?:适量|少许|少量|若干|足量)\s*(?:{_COUNT_UNIT})?\s*$"
)
#: 时间/温度后缀："冷冻4小时" / "冬35度"
_TIME_SUFFIX = re.compile(rf"{_NUMBER}\s*(?:小时|分钟|秒|度|℃)\s*$")
#: 切分处理后缀："1切4" / "1开2"
_PROCESS_SUFFIX = re.compile(rf"{_NUMBER}\s*(?:切|开|分|掰)\s*\d*\s*$")
#: 近似词后缀："50克左右"
_APPROX_SUFFIX = re.compile(r"(?:左右|上下|以内|以上)$")
#: 尾部裸数字（"淀粉3" / "黄油95"）
_BARE_DIGIT_SUFFIX = re.compile(rf"{_NUMBER}\s*$")
#: 未配对括号残留 / 尾部括号
_PAREN_OPEN_UNMATCHED = re.compile(r"[（(][^）)]*$")
_PAREN_CLOSE_TAIL = re.compile(r"[）)]+$")

#: 尾部量词（无数字）
_QTY_WORDS = ("适量", "少许", "少許", "少量", "若干", "足量")

#: 括号内处理语，反复去掉最内层括号
_PAREN_GROUP_RE = re.compile(r"[（(][^（()）]*[)）]")

#: 切配形态后缀（form 属性）
FORM_SUFFIXES = ("丝", "片", "末", "蓉", "丁", "花", "碎", "块", "粒", "段", "条", "泥")
#: 以形态字符结尾但本身是完整食材名的整名（不按形态处理）
_WHOLE_NAMES_WITH_FORM_CHAR = {
    "丝瓜",
    "蒜苔",
    "豆苗",
    "西红柿",
    "马齿苋",
    "蟹味菇",
    "油条",
}

# 固定源中有少量破损的双层括号把“切3cm段））/切丝））/切块））”拆成
# 独立片段。它们是处理说明，不是食材出现。
_INSTRUCTION_ONLY_RE = re.compile(
    r"^(?:"
    r"切(?:\d+(?:\.\d+)?\s*(?:cm|厘米|毫米))?(?:丝|片|末|蓉|丁|花|碎|块|粒|段|条|泥)?"
    r"|切小块"
    r"|洗净(?:划花刀|切\d+段)?"
    r"|去皮"
    r"|去内脏洗净"
    r"|冷冻\d+(?:\.\d+)?小时"
    r"|根和叶分开"
    r"|去虾须"
    r"|去脚"
    r"|取净肉"
    r"|包子皮材料"
    r"|肉馅材料"
    r")[）)]*$"
)

# 固定源中把多个实际食材连写成制备混合物的封闭清单。
_FIXED_COMPOUND_SPLITS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "蜂蜜加油混合": (("蜂蜜", None), ("油", None)),
    "青红椒": (("青椒", None), ("红椒", None)),
    "青红椒丝": (("青椒", "丝"), ("红椒", "丝")),
    "葱姜": (("葱", None), ("姜", None)),
    "姜葱": (("姜", None), ("葱", None)),
    "姜葱末": (("姜", "末"), ("葱", "末")),
    "葱姜蒜": (("葱", None), ("姜", None), ("蒜", None)),
    "葱姜蒜各": (("葱", None), ("姜", None), ("蒜", None)),
    "葱姜汁": (("葱", "汁"), ("姜", "汁")),
    "葱姜水": (("葱", None), ("姜", None), ("水", None)),
}

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
    form: str | None = None
    is_note: bool = False


def _detect_group(fragment: str) -> tuple[str | None, str]:
    match = GROUP_PREFIX_RE.match(fragment)
    if match:
        group = match.group(0).rstrip("：:")
        return group, fragment[match.end() :].strip()
    return None, fragment


def _check_optional(text: str) -> bool:
    return any(marker.search(text) for marker in OPTIONAL_MARKERS)


def _strip_parens(name: str) -> str:
    """反复去掉最内层括号及其内容（处理语/说明）。可选标记已先行判定。"""
    cleaned = name
    while True:
        match = _PAREN_GROUP_RE.search(cleaned)
        if not match:
            break
        cleaned = cleaned[: match.start()] + cleaned[match.end() :]
    return cleaned.strip()


def _strip_quantities(name: str) -> str:
    """剥离数量/单位/范围/时间/处理语/括号残留；保留 T55面粉、80头干瑶柱 等合法数字名。

    只剥"数字+度量单位"前缀与"数字+单位"后缀；中段数字（T55、80头）不剥。
    每次迭代先剥尾部括号/分隔符/近似词残留，使后缀数量可再次匹配。
    """
    cleaned = name
    changed = True
    while changed:
        changed = False
        # 先剥尾部残留，让后缀数量可再匹配
        for pattern in (_PAREN_CLOSE_TAIL, _APPROX_SUFFIX, re.compile(r"[~\-—/+]+$")):
            stripped = pattern.sub("", cleaned).strip()
            if stripped != cleaned:
                cleaned = stripped
                changed = True
        prefix = _QTY_PREFIX.match(cleaned)
        if prefix:
            cleaned = cleaned[prefix.end() :].strip()
            changed = True
        for pattern in (
            _QTY_SUFFIX,
            _CHINESE_QTY_SUFFIX,
            _QUALITATIVE_QTY_SUFFIX,
            _RANGE_WITH_UNIT,
            _RANGE_SUFFIX,
            _TIME_SUFFIX,
            _PROCESS_SUFFIX,
        ):
            stripped = pattern.sub("", cleaned)
            if stripped != cleaned:
                cleaned = stripped
                changed = True
        stripped = _PAREN_OPEN_UNMATCHED.sub("", cleaned)
        if stripped != cleaned:
            cleaned = stripped
            changed = True
    # 尾部裸数字/不完整分数
    cleaned = _BARE_DIGIT_SUFFIX.sub("", cleaned).strip()
    cleaned = re.sub(r"[（(]?\d+\/\s*$", "", cleaned).strip()
    # 纯量词
    cleaned = re.sub(r"^(?:约|大约|适量|少许|少許|少量|若干|足量)\s*", "", cleaned).strip()
    for qty_word in _QTY_WORDS:
        if cleaned.endswith(qty_word) and len(cleaned) > len(qty_word):
            cleaned = cleaned[: -len(qty_word)].strip()
    # 固定源用“各”表达同组共用数量（白糖各/水淀粉各），不属于食材身份。
    cleaned = cleaned.removesuffix("各").strip()
    return cleaned.strip("，,;；。/ ")


def _extract_qty_name(text: str) -> tuple[str, str | None, str | None]:
    """返回 (食材名, 数量原文, 单位原文)。"""
    suffix = _QTY_SUFFIX.search(text)
    chinese_suffix = _CHINESE_QTY_SUFFIX.search(text)
    prefix = _QTY_PREFIX.match(text)
    qty_raw = None
    if suffix:
        qty_raw = suffix.group().strip()
    elif chinese_suffix:
        qty_raw = chinese_suffix.group().strip()
    elif prefix:
        qty_raw = prefix.group().strip()
    unit_raw = None
    if qty_raw:
        unit_match = re.search(rf"({_UNIT})$", qty_raw)
        unit_raw = unit_match.group() if unit_match else None
    return _strip_quantities(text), qty_raw, unit_raw


def _detect_form(name: str) -> tuple[str, str | None]:
    """返回 (名称, 切配形态)。整名如 丝瓜/蒜苔 不视为形态变体。"""
    if name in _WHOLE_NAMES_WITH_FORM_CHAR:
        return name, None
    for suffix in FORM_SUFFIXES:
        if name.endswith(suffix) and len(name) - len(suffix) >= 1:
            return name[: -len(suffix)], suffix
    return name, None


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
    buffer: list[str] = []
    parenthesis_depth = 0
    top_level_parts: list[str] = []
    for character in ingredients_raw:
        if character in "（(":
            parenthesis_depth += 1
        elif character in "）)" and parenthesis_depth:
            parenthesis_depth -= 1
        if character in _SPLIT_CHARS and parenthesis_depth == 0:
            top_level_parts.append("".join(buffer))
            buffer.clear()
        else:
            buffer.append(character)
    top_level_parts.append("".join(buffer))

    for p in top_level_parts:
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

        if _INSTRUCTION_ONLY_RE.fullmatch(body.strip()):
            occurrences.append(
                ParsedOccurrence(raw_text=fragment, name_clean=body, group=group, is_note=True)
            )
            continue

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
                cleaned = _strip_parens(alt)
                clean_name, qty, unit = _extract_qty_name(cleaned)
                if not clean_name:
                    continue  # 纯处理语残留（如"1切4"），不是食材
                base, form = _detect_form(clean_name)
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
                        form=form,
                    )
                )
        else:
            cleaned = _strip_parens(body)
            clean_name, qty, unit = _extract_qty_name(cleaned)
            if not clean_name:
                continue  # 纯处理语残留，不是食材
            compound_names = _FIXED_COMPOUND_SPLITS.get(clean_name)
            if compound_names:
                for compound_name, compound_form in compound_names:
                    occurrences.append(
                        ParsedOccurrence(
                            raw_text=fragment,
                            name_clean=compound_name,
                            group=group,
                            is_optional=is_optional,
                            form=compound_form,
                        )
                    )
                continue
            base, form = _detect_form(clean_name)
            occurrences.append(
                ParsedOccurrence(
                    raw_text=fragment,
                    name_clean=clean_name,
                    group=group,
                    quantity_raw=qty,
                    unit_raw=unit,
                    is_optional=is_optional,
                    form=form,
                )
            )

    return occurrences
