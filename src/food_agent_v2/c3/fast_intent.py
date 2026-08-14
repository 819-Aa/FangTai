"""C3 快速意图路由（P3/L1.1）—— 类型化、fail-closed 的确定性意图识别。

替代 query_understanding 模型。只对**唯一且明确**的表达生成确定性 delta；
出现健康语义（过敏/疾病/指标/不能吃）但无法由 B2 信号格式唯一表达时，设置
``intent="model_fallback"`` 与 ``unresolved_health_text``，由上层调用一次
QueryNormalizer 或转 needs_clarification，绝不当作普通推荐继续。

健康关键词只能触发安全路由（model_fallback / needs_clarification），不得直接
生成疾病医学结论。口味偏好（"别太甜"）走 preference_exclusions，不进健康约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

IntentKind = Literal[
    "new_recommendation",
    "add_constraint",
    "replace",
    "reject_plan",
    "restore",
    "conflict",
    "needs_clarification",
    "model_fallback",
]

#: 中文数字 → 整数（用于"三菜一汤"等菜数提取）。
_CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cn_num_to_int(text: str) -> int | None:
    """中文数字 → 整数（"十"→10、"十五"→15、"二十"→20、"三"→3）。"""
    if text in _CN_NUM:
        return _CN_NUM[text]
    if "十" in text:
        parts = text.split("十")
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return None

#: 健康语言（过敏/疾病/指标/不能吃）——命中即 model_fallback，不得普通推荐。
_HEALTH_LANGUAGE = (
    "过敏", "不能吃", "忌口", "忌", "不耐受",
    "血压", "血糖", "血脂", "尿酸", "胆固醇",
    "糖尿病", "高血压", "痛风", "肾病", "心脏病", "脂肪肝",
)

#: 口味偏好（软排除，不进健康约束，走 C1 软排序）。
_PREFERENCE_EXCLUSIONS = {
    "甜": ("别太甜", "不要太甜", "不吃甜", "少糖", "少吃甜"),
    "清淡": ("清淡一点", "清爽一点", "少油", "少盐"),
}

#: 明确食材禁忌 → 标准食材名（B2 严格解析要求标准 ingredient 名）。
_TABOO_MAP = {
    "辣椒": ("不吃辣", "别做辣", "不要辣", "一点辣都不想碰", "不吃辣椒", "别放辣椒"),
    "虾": ("不吃虾", "不要虾", "别放虾"),
    "香菜": ("不吃香菜", "不要香菜"),
}

#: 多轮替换/否定/恢复指示词。
_REPLACE_HINTS = ("换掉", "换成", "改成", "替换", "不要这道", "去掉")
_REJECT_HINTS = ("重新推荐", "换一批", "再来", "重来", "不要这个方案", "推翻")
_RESTORE_HINTS = ("上一版", "刚才的", "之前那个", "恢复", "回到之前", "原来那版")
#: 约束追加指示（含禁忌/偏好追加词，非全新推荐）。
_ADD_HINTS = ("别做", "不吃", "别太", "清淡一点", "减脂", "少放", "少油", "少盐", "少糖", "别放")


@dataclass(frozen=True)
class IntentDelta:
    """FastIntentRouter 输出：本轮意图 + 可结构化表达。"""

    intent: IntentKind = "new_recommendation"
    query: str = ""
    dish_count_requested: int | None = None
    flavor_preferences: tuple[str, ...] = ()
    dish_types: tuple[str, ...] = ()
    health_exclusions: tuple[str, ...] = ()
    preference_exclusions: tuple[str, ...] = ()
    time_constraint_seconds: int | None = None
    time_constraint_policy: str = "flexible"
    target_recipe_id: int | None = None
    target_slot: str | None = None
    preserve_unmentioned_items: bool = True
    clarification_reason: str | None = None
    unresolved_health_text: str | None = None


def _dish_count(message: str) -> int | None:
    """从"三菜一汤/四菜一汤/N道菜"提取菜数；无法判定返回 None（C2 用默认）。"""
    m = re.search(r"([一二两三四五六七八九十])菜一汤", message)
    if m:
        return _CN_NUM.get(m.group(1), 0) + 1
    m = re.search(r"([一二两三四五六七八九十])\s*菜", message)
    if m and "汤" in message:
        return _CN_NUM.get(m.group(1), 0) + 1
    m = re.search(r"(\d+)\s*道菜", message)
    if m:
        return int(m.group(1))
    return None


def _time_constraint(message: str) -> tuple[int | None, str]:
    """提取时间约束。半小时/N分钟/中文数字分钟 → hard；"尽量快" → flexible。"""
    if "半小时" in message:
        return 1800, "hard"
    if "一刻钟" in message:
        return 900, "hard"
    m = re.search(r"(\d+)\s*分钟", message)
    if m:
        return int(m.group(1)) * 60, "hard"
    m = re.search(r"([一二两三四五六七八九十]+)\s*分钟", message)
    if m:
        minutes = _cn_num_to_int(m.group(1))
        if minutes is not None:
            return minutes * 60, "hard"
    if any(k in message for k in ("尽量快", "快一点", "快点", "尽快", "时间短")):
        return None, "flexible"
    return None, "flexible"


def _detect_health_language(message: str) -> str | None:
    """检测过敏/疾病/指标/不能吃语言；命中返回关键词，否则 None。"""
    for kw in _HEALTH_LANGUAGE:
        if kw in message:
            return kw
    return None


def _has_relative_conflict(message: str) -> bool:
    """相对称谓多人矛盾（"一个人…另一个人…"或多次"一个人"）。"""
    if message.count("一个人") >= 2:
        return True
    if "另一个人" in message or "其他人" in message:
        return True
    return False


#: 相对称谓（多人角色），参与者归属不唯一，需澄清，不得错误绑定 p1。
_RELATIVE_PERSON_HINTS = ("小孩", "老人", "孩子", "宝宝", "小朋友", "长辈", "爸爸", "妈妈")


def _has_relative_person(message: str) -> bool:
    """相对称谓多人角色（小孩/老人等）→ 归属不唯一，需澄清。"""
    return any(h in message for h in _RELATIVE_PERSON_HINTS)


def _taboo_exclusions(message: str, participant_ref: str) -> tuple[str, ...]:
    """明确食材禁忌 → B2 临时信号格式（`参与者N:禁忌:值`），由 B2 严格解析闭合。"""
    out: list[str] = []
    for taboo, hints in _TABOO_MAP.items():
        if any(h in message for h in hints):
            out.append(f"{participant_ref}:禁忌:{taboo}")
    return tuple(out)


def _preference_exclusions(message: str) -> tuple[str, ...]:
    """口味偏好（软排除），不进健康约束。"""
    out: list[str] = []
    for pref, hints in _PREFERENCE_EXCLUSIONS.items():
        if any(h in message for h in hints):
            out.append(pref)
    return tuple(out)


def _flavor_preferences(message: str) -> tuple[str, ...]:
    """常见口味/风格词 → 偏好（软目标）。"""
    flavors = []
    for kw in ("家常", "清淡", "清爽", "暖胃", "补气血", "热乎", "有仪式感", "下饭"):
        if kw in message:
            flavors.append(kw)
    return tuple(flavors)


def _dish_types(message: str) -> tuple[str, ...]:
    """明确结构需求。只提取"想吃面"类主食，不提取"汤"（"四菜一汤"是菜数）。"""
    if any(k in message for k in ("想吃面", "面条", "面食", "煮面")):
        return ("主食",)
    return ()


class FastIntentRouter:
    """确定性意图路由器：无外部网络依赖，只解析明确表达的语义。"""

    @staticmethod
    def route(message: str, participant_refs: tuple[str, ...] = ("p1",)) -> IntentDelta:
        text = (message or "").strip()
        first_ref = participant_refs[0] if participant_refs else "p1"

        # 1. 健康语言（过敏/疾病/指标/不能吃）→ model_fallback（不普通推荐）
        health = _detect_health_language(text)
        if health is not None:
            return IntentDelta(
                intent="model_fallback", query=text, unresolved_health_text=health)

        # 2. 相对称谓多人矛盾 → conflict（生成可解释澄清问题）
        if _has_relative_conflict(text):
            return IntentDelta(
                intent="conflict", query=text,
                clarification_reason="多人约束互相矛盾，需要澄清")

        # 2.5 相对称谓多人角色（小孩/老人等）→ needs_clarification（归属不唯一）
        if _has_relative_person(text):
            return IntentDelta(
                intent="needs_clarification", query=text,
                clarification_reason="多人相对称谓需澄清参与者归属")

        # 3. 明确多轮意图（替换/否定/恢复）
        if any(h in text for h in _REPLACE_HINTS):
            return IntentDelta(intent="replace", query=text,
                               preserve_unmentioned_items=True)
        if any(h in text for h in _REJECT_HINTS):
            return IntentDelta(intent="reject_plan", query=text)
        if any(h in text for h in _RESTORE_HINTS):
            return IntentDelta(intent="restore", query=text)

        # 4. 约束追加（含禁忌/偏好追加词）或首次推荐
        intent_kind = "add_constraint" if any(h in text for h in _ADD_HINTS) \
            else "new_recommendation"
        return IntentDelta(
            intent=intent_kind,
            query=text,
            dish_count_requested=_dish_count(text),
            flavor_preferences=_flavor_preferences(text),
            dish_types=_dish_types(text),
            health_exclusions=_taboo_exclusions(text, first_ref),
            preference_exclusions=_preference_exclusions(text),
            time_constraint_seconds=_time_constraint(text)[0],
            time_constraint_policy=_time_constraint(text)[1],
        )
