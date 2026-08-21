"""把菜谱原步骤确定性拆成稳定原子，并只解析原文明确时长。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from food_agent_v2.b1.schemas import StepAtom

OVERNIGHT_SECONDS = 8 * 60 * 60

_NUMBER = r"\d+(?:\.\d+)?"
_RANGE_RE = re.compile(
    rf"(?P<low>{_NUMBER})\s*(?:-|–|—|~|～|至|到)\s*"
    rf"(?P<high>{_NUMBER})\s*(?P<unit>小时|钟头|分钟|分|秒钟|秒)"
)
_EXACT_RE = re.compile(rf"(?P<value>{_NUMBER})\s*(?P<unit>小时|钟头|分钟|分|秒钟|秒)")
_SPECIAL_DURATIONS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"四分之一\s*(?:个)?小时"), 15 * 60),
    (re.compile(r"一刻钟"), 15 * 60),
    (re.compile(r"半\s*(?:个)?小时"), 30 * 60),
    (re.compile(r"半分钟"), 30),
    (re.compile(r"隔夜"), OVERNIGHT_SECONDS),
)
_UNIT_SECONDS = {
    "小时": 3600,
    "钟头": 3600,
    "分钟": 60,
    "分": 60,
    "秒钟": 1,
    "秒": 1,
}

_NON_TASK_RE = re.compile(
    r"^(?:"
    r"准备好(?:所有|全部)?(?:的)?食材|"
    r"食材准备|准备食材|"
    r"(?:放(?:在)?一旁)?备用|"
    r"成品展示|尽情品尝吧?|"
    r"(?:烹饪|制作)?(?:结束|完成)(?:后)?"
    r"(?:[，,]?(?:即可|趁热)?(?:食用|享用|品尝)(?:吧)?)?|"
    r"结束后|"
    r"(?:装盘|盛出)(?:后)?(?:即可)?(?:尽情)?(?:食用|享用|品尝)(?:吧)?|"
    r"(?:即可|趁热)(?:食用|享用|品尝)(?:吧)?|"
    r"尽情享用|大功告成"
    r")$"
)
_NON_TASK_NARRATIVE_RE = re.compile(
    r"^(?:"
    r".*(?:享用|享受|品尝|来分享).*|"
    r".*(?:就)?做好(?:了|啦)(?:[！!~].*)?|"
    r".*(?:搞定|完成)(?:啦|了)?(?:[，,].*)?|"
    r"(?:烹饪|烘烤)(?:结束|完成).*|"
    r"(?:可|即可|取出|完成后).*?(?:食用|饮用).*|"
    r"成品|待用|"
    r"(?:烹饪|烘烤|烤制|蒸制)(?:结束|完成)(?:后)?|"
    r"\d+\s*℃|设置：|普通蒸模式|常规烘焙模式|无需预热|9分满即可"
    r")$"
)
_NON_TASK_NOTE_RE = re.compile(
    r"^(?:"
    r"[（(](?:量的要求|由于|或用|室温即可|如使用|切记|8寸原料).*|"
    r"[）)]|"
    r"提示：.*|此步骤可用.*|用量根据.*|羊排选.*|最后烤制目的是.*|"
    r"视室温.*|如果希望.*|可不放|这样省的.*|"
    r"方太.*(?:蒸箱|烤箱)|"
    r"\d+寸原料是.*|可做.*|此时为第.*|总共要进行.*|"
    r"切好的.*|包完.*后|.*后的样子|若.*|恨不得.*|"
    r"香菇多汁.*|\x13\x04|"
    r"(?:美味的|简单却美味的|完美的|酒香四溢的|可蘸酱油食用的).+|"
    r"健康又美味|口感极佳|美味的早餐|清香脆口"
    r")$"
)
_PASSIVE_WAIT_RE = re.compile(
    r"^(?:"
    r"发酵(?!结束)|开始发酵|室温发酵|冷冻|冷藏|"
    r"(?:再)?放入冰箱|进冰箱|盖.*(?:醒发|发酵)|"
    r"静置|浸泡|泡发|腌制|醒发|松弛|"
    r"至(?:其|食材)?(?:入味|熟透|熟软)|使.*(?:变硬|定型)|"
    r"等到.*(?:发好|发酵)|"
    r"待(?:烹饪|烘烤|烤制|蒸制|预热)(?:结束|完成)(?:后)?|预热结束后"
    r")"
)
_PASSIVE_TIMED_MARKER_RE = re.compile(
    r"发酵|冷冻|冷藏|浸泡|泡发|腌制|醒发|静置|松弛"
)
_DEVICE_COMPOUND_RE = re.compile(
    r"^(?P<startup>.*?(?:放入|送入|移入|置入|装入)"
    r"(?:预热(?:好|至[^，,]+)的?)?(?:烤箱|蒸箱|微波炉))"
    r"\s*(?:，|,|并(?:且)?|后|然后|再)+\s*"
    r"(?P<run>(?:烤|烘烤|焗|蒸|微波).+)$"
)
_OUTER_PUNCTUATION_RE = re.compile(r"^[\s，,；;。.!！？?、]+|[\s，,；;。.!！？?、]+$")
_SPACE_RE = re.compile(r"\s+")
_CLAUSE_SPLIT_RE = re.compile(r"[，,；;]+")


def parse_explicit_duration(text: str) -> int | None:
    """解析步骤中全部明确时长并求和；范围使用数学中点。"""
    if not text:
        return None
    masked = list(text)
    seconds = Decimal(0)
    found = False

    def consume(match: re.Match[str], value: Decimal) -> None:
        nonlocal seconds, found
        seconds += value
        found = True
        masked[match.start() : match.end()] = " " * (match.end() - match.start())

    for pattern, value in _SPECIAL_DURATIONS:
        for match in pattern.finditer("".join(masked)):
            consume(match, Decimal(value))

    for match in _RANGE_RE.finditer("".join(masked)):
        low = Decimal(match.group("low"))
        high = Decimal(match.group("high"))
        midpoint = (low + high) / Decimal(2)
        consume(match, midpoint * _UNIT_SECONDS[match.group("unit")])

    for match in _EXACT_RE.finditer("".join(masked)):
        value = Decimal(match.group("value")) * _UNIT_SECONDS[match.group("unit")]
        consume(match, value)

    if not found:
        return None
    return int(seconds.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def is_non_task_text(text: str) -> bool:
    """识别不代表真实烹饪工作的准备/备用/享用提示语。"""
    normalized = _normalize_atom_text(text)
    return bool(
        _NON_TASK_RE.fullmatch(normalized)
        or _NON_TASK_NARRATIVE_RE.fullmatch(normalized)
        or _NON_TASK_NOTE_RE.fullmatch(normalized)
    )


def is_passive_wait_text(text: str) -> bool:
    """识别以等待为主、应占 elapsed 但不持续占用厨师的步骤。"""
    normalized = _normalize_atom_text(text)
    return bool(
        _PASSIVE_WAIT_RE.match(normalized)
        or (
            parse_explicit_duration(normalized) is not None
            and _PASSIVE_TIMED_MARKER_RE.search(normalized)
        )
    )


def atomize_step(*, recipe_id: int, source_step_index: int, text: str) -> tuple[StepAtom, ...]:
    """拆分单条源步骤，并生成由内容决定的稳定 atom ID。"""
    normalized = _normalize_atom_text(text)
    if not normalized:
        return ()
    clauses = tuple(
        sub_clause
        for timed_clause in _split_timed_compound(normalized)
        for sub_clause in _split_startup_and_unattended_run(timed_clause)
    )
    atoms: list[StepAtom] = []
    clause_counts: dict[str, int] = {}
    for clause in clauses:
        clause_counts[clause] = clause_counts.get(clause, 0) + 1
        duration = 0 if is_non_task_text(clause) else parse_explicit_duration(clause)
        atoms.append(
            StepAtom(
                atom_id=_atom_id(
                    recipe_id,
                    source_step_index,
                    clause,
                    occurrence=clause_counts[clause],
                ),
                source_step_index=source_step_index,
                text=clause,
                explicit_duration_seconds=duration,
                duration_locked=duration is not None,
            )
        )
    if len({atom.atom_id for atom in atoms}) != len(atoms):
        raise ValueError(
            f"recipe_id={recipe_id} source_step_index={source_step_index} 产生重复原子"
        )
    return tuple(atoms)


def atomize_recipe_steps(
    *, recipe_id: int, steps: Iterable[tuple[int, str]]
) -> tuple[StepAtom, ...]:
    """按源步骤顺序原子化整道菜。"""
    return tuple(
        atom
        for source_step_index, text in steps
        for atom in atomize_step(
            recipe_id=recipe_id,
            source_step_index=source_step_index,
            text=text,
        )
    )


def ordered_atoms_hash(atoms: Iterable[StepAtom]) -> str:
    """生成与 build ID 无关、对 atom 顺序敏感的内容散列。"""
    material = "\n".join(
        "\x1f".join(
            (
                atom.atom_id,
                str(atom.source_step_index),
                _normalize_atom_text(atom.text),
                "" if atom.explicit_duration_seconds is None else str(atom.explicit_duration_seconds),
                "1" if atom.duration_locked else "0",
            )
        )
        for atom in atoms
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _split_startup_and_unattended_run(text: str) -> tuple[str, ...]:
    match = _DEVICE_COMPOUND_RE.fullmatch(text)
    if match is None:
        return (text,)
    return (
        _normalize_atom_text(match.group("startup")),
        _normalize_atom_text(match.group("run")),
    )


def _split_timed_compound(text: str) -> tuple[str, ...]:
    """Split comma-separated work when one clause carries an explicit duration."""
    clauses = tuple(
        normalized
        for clause in _CLAUSE_SPLIT_RE.split(text)
        if (normalized := _normalize_atom_text(clause))
    )
    if len(clauses) <= 1 or not any(parse_explicit_duration(clause) is not None for clause in clauses):
        return (text,)
    return clauses


def _normalize_atom_text(text: str) -> str:
    stripped = _OUTER_PUNCTUATION_RE.sub("", text or "")
    return _SPACE_RE.sub(" ", stripped).strip()


def _atom_id(
    recipe_id: int,
    source_step_index: int,
    text: str,
    *,
    occurrence: int = 1,
) -> str:
    material = f"{recipe_id}\x1f{source_step_index}\x1f{_normalize_atom_text(text)}"
    if occurrence > 1:
        material += f"\x1f{occurrence}"
    return "atom_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
