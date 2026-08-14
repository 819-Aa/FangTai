"""C3 快速意图路由（P3）—— 确定性意图识别，替代 query_understanding 模型。

只解析用户**明确表达**的语义；无法唯一解析的复杂/歧义表达由编排器决定
返回 needs_clarification 或调用 QueryNormalizer 模型兜底。疾病、指标和过敏
信号仍须经 B2 严格解析，本模块不做关键词→健康结论的越权决定。

设计文档 §5.1：公开用例只决定优化优先级，能力边界由通用语义规则覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 中文数字 → 整数（用于"三菜一汤"等菜数提取）。
_CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

#: 明确的多轮替换/否定指示词（命中则 fallback legacy / 走 P5 多轮 delta）。
_REPLACE_HINTS = ("换掉", "换成", "改成", "替换", "不要这道", "去掉")
_REJECT_HINTS = ("重新推荐", "换一批", "再来", "重来", "不要这个方案", "推翻")

#: 明确禁忌 → 标准排除项（参与者归属由 caller 提供；此处产出相对条目）。
_TABOO_MAP = {
    "辣": ("不吃辣", "别做辣", "不要辣", "一点辣都不想碰", "不吃辣椒"),
    "海鲜": ("不吃海鲜", "不要海鲜", "别放海鲜"),
    "甜": ("别太甜", "不要太甜", "不吃甜"),
    "油腻": ("别太油", "不要油腻", "不吃油腻", "少油"),
    "牛羊肉": ("不吃牛", "不吃羊", "不吃牛羊肉"),
}


@dataclass
class IntentDelta:
    """FastIntentRouter 输出：本轮意图 + 可结构化表达。"""

    intent: str = "new_recommendation"
    query: str = ""
    dish_count_requested: int | None = None
    flavor_preferences: tuple[str, ...] = ()
    dish_types: tuple[str, ...] = ()
    health_exclusions: tuple[str, ...] = ()
    preference_exclusions: tuple[str, ...] = ()
    time_constraint_seconds: int | None = None
    time_constraint_policy: str = "flexible"
    clarification_reason: str | None = None


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
    """提取时间约束。半小时/N分钟 → hard；"尽量快/快一点" → flexible 软偏好。"""
    if "半小时" in message:
        return 1800, "hard"
    m = re.search(r"(\d+)\s*分钟", message)
    if m:
        return int(m.group(1)) * 60, "hard"
    if any(k in message for k in ("尽量快", "快一点", "快点", "尽快", "时间短")):
        return None, "flexible"
    return None, "flexible"


def _taboo_exclusions(message: str, participant_ref: str) -> tuple[str, ...]:
    """明确禁忌 → B2 临时信号格式（`参与者N:禁忌:值`），由 B2 严格解析闭合。"""
    out: list[str] = []
    for taboo, hints in _TABOO_MAP.items():
        if any(h in message for h in hints):
            out.append(f"{participant_ref}:禁忌:{taboo}")
    return tuple(out)


def _flavor_preferences(message: str) -> tuple[str, ...]:
    """常见口味/风格词 → 偏好（软目标，不进健康约束）。"""
    flavors = []
    for kw in ("家常", "清淡", "清爽", "暖胃", "补气血", "热乎", "有仪式感", "下饭"):
        if kw in message:
            flavors.append(kw)
    return tuple(flavors)


def _dish_types(message: str) -> tuple[str, ...]:
    """明确结构需求。第一版只提取明确的"想吃面"类主食需求，不提取"汤"——
    "四菜一汤"的"汤"是菜数描述，过度提取会触发 require_soup 导致 C2 no_feasible_menu。
    """
    if any(k in message for k in ("想吃面", "面条", "面食", "煮面")):
        return ("主食",)
    return ()


def _intent(message: str) -> str:
    """确定性意图分类。明确替换/否定走 P5 多轮 delta（此处标记，编排器分流）。"""
    if any(h in message for h in _REPLACE_HINTS):
        return "replace"
    if any(h in message for h in _REJECT_HINTS):
        return "reject_plan"
    return "new_recommendation"


class FastIntentRouter:
    """确定性意图路由器：无外部网络依赖，只解析明确表达的语义。"""

    @staticmethod
    def route(message: str, participant_ref: str = "p1") -> IntentDelta:
        text = (message or "").strip()
        d = IntentDelta(query=text)
        d.dish_count_requested = _dish_count(text)
        d.time_constraint_seconds, d.time_constraint_policy = _time_constraint(text)
        d.health_exclusions = _taboo_exclusions(text, participant_ref)
        d.flavor_preferences = _flavor_preferences(text)
        d.dish_types = _dish_types(text)
        d.intent = _intent(text)
        return d
