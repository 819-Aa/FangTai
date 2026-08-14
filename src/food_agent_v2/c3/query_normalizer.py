"""C3 单次 QueryNormalizer 模型兜底（L1.2）。

只有 FastIntentRouter 无法形成无歧义 IntentDelta（model_fallback）时才调用一次
模型。无工具、单次调用、严格 JSON、无自修复循环；超时/空输出/Schema 错误统一
转 needs_clarification，不进入完整推荐链路。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from food_agent_v2.c3.fast_intent import IntentDelta
from food_agent_v2.c3.llm_client import get_llm_client

_SYSTEM_PROMPT = (
    "你是膳食推荐系统的意图归一化器。把用户的复杂、模糊或含健康语义的表达"
    "归一化为结构化意图。只输出一个 JSON 对象，字段如下：\n"
    '  "intent": 字符串，取值 new_recommendation / add_constraint / replace / '
    'reject_plan / restore / conflict / needs_clarification\n'
    '  "dish_count_requested": 整数或 null（菜数）\n'
    '  "flavor_preferences": 字符串数组（口味偏好）\n'
    '  "health_exclusions": 字符串数组（健康排除，格式"参与者N:禁忌:食材"，'
    "食材用标准名）\n"
    '  "time_constraint_seconds": 整数或 null\n'
    '  "time_constraint_policy": "flexible" 或 "hard"\n'
    '  "target_recipe_id": 整数或 null（替换目标菜）\n'
    "无法唯一解析时 intent 用 needs_clarification 并给出简短 reason。"
    "只输出 JSON，不要解释。"
)

_VALID_INTENTS = {
    "new_recommendation", "add_constraint", "replace", "reject_plan",
    "restore", "conflict", "needs_clarification",
}


def _build_user_message(message: str, participant_refs: Sequence[str],
                        current_menu: dict | None) -> str:
    refs = list(participant_refs)
    menu = current_menu.get("recipe_ids") if current_menu else None
    return json.dumps({
        "message": message,
        "participant_refs": refs,
        "current_menu_recipe_ids": menu,
    }, ensure_ascii=False)


def _parse_json(content: str) -> dict | None:
    if isinstance(content, dict):
        return content
    if not content:
        return None
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _to_intent_delta(data: dict, message: str) -> IntentDelta:
    intent = data.get("intent", "needs_clarification")
    if intent not in _VALID_INTENTS:
        intent = "needs_clarification"
    return IntentDelta(
        intent=intent,  # 运行时 Literal 只是类型标注，直接传字符串
        query=message,
        dish_count_requested=data.get("dish_count_requested"),
        flavor_preferences=tuple(data.get("flavor_preferences") or ()),
        health_exclusions=tuple(data.get("health_exclusions") or ()),
        time_constraint_seconds=data.get("time_constraint_seconds"),
        time_constraint_policy=data.get("time_constraint_policy", "flexible"),
        target_recipe_id=data.get("target_recipe_id"),
        clarification_reason=data.get("clarification_reason") or data.get("reason"),
    )


class QueryNormalizer:
    """单次无工具模型归一化；失败统一转 needs_clarification。"""

    def __init__(self, llm=None) -> None:
        self._llm = llm or get_llm_client()

    def normalize(
        self,
        message: str,
        participant_refs: Sequence[str],
        current_menu: dict | None = None,
        timeout_seconds: float = 3.0,
    ) -> IntentDelta:
        try:
            response = self._llm.invoke(
                "query_understanding",
                _SYSTEM_PROMPT,
                _build_user_message(message, participant_refs, current_menu),
                response_format={"type": "json_object"},
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            return IntentDelta(
                intent="needs_clarification", query=message,
                clarification_reason="归一化模型调用失败")

        data = _parse_json(response.get("content", ""))
        if data is None:
            return IntentDelta(
                intent="needs_clarification", query=message,
                clarification_reason="归一化模型输出非 JSON")
        return _to_intent_delta(data, message)
