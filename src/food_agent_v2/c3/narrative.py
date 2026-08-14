"""C3 NarrativePolisher（可选润色，L3 Deferred Backlog）。

只润色公开解释（conclusion/menu_summary/reasoning_summary 三个文字字段），
不回改菜单身份、菜名、recipe_ids、健康结论或时间权威性。默认关闭；启用时
只在剩余预算足够（≥2.5s）时调用（最多 2.0s），失败/超时/非 JSON/校验不通过
直接回退确定性回答，不改变请求成功状态。这是一种受控的"表现层降级"。
"""

from __future__ import annotations

import json
import os
import re

from food_agent_v2.c3.llm_client import get_llm_client
from food_agent_v2.contracts.artifacts import AnswerContent
from food_agent_v2.contracts.build import canonical_json_hash

_POLISH_SYSTEM_PROMPT = (
    "你是膳食推荐的回答润色器。润色下面的回答，使其更自然、更有解释力。"
    "只输出一个 JSON 对象，只能包含 conclusion / menu_summary / reasoning_summary "
    "三个字段；菜名必须与原文完全一致，不得新增菜品、疾病或医学结论。"
    "只输出 JSON，不要解释。"
)


def narrative_polish_enabled() -> bool:
    return os.getenv("NARRATIVE_POLISH_ENABLED", "false").lower() == "true"


def _parse_json(content):
    if isinstance(content, dict):
        return content
    if not content:
        return None
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _validate_polish_fields(original, data) -> bool:
    """输出审计：三个字段必须字符串、无医学结论、菜名原样保留。违反返回 False。"""
    from food_agent_v2.c3.fast_intent import _detect_health_language

    keys = ("conclusion", "menu_summary", "reasoning_summary")
    # 1. 类型：只接受字符串（异常类型会令 AnswerContent 抛 ValidationError）
    for key in keys:
        val = data.get(key)
        if val is not None and not isinstance(val, str):
            return False
    # 2. 禁止医学结论（疾病/指标/过敏词，不得出现在润色文本）
    for key in keys:
        val = data.get(key)
        if isinstance(val, str) and _detect_health_language(val) is not None:
            return False
    # 3. 菜名原样保留（最终合并后的文本必须含原菜单每个菜名，模型不得改菜名）
    original_names = [n.strip() for n in
                      re.split(r"[、,，]", original.content.menu_summary or "") if n.strip()]
    final_summary = data.get("menu_summary") or original.content.menu_summary
    final_conclusion = data.get("conclusion") or original.content.conclusion
    new_text = (final_summary or "") + (final_conclusion or "")
    return all(name in new_text for name in original_names)


def _content_hash(answer) -> str:
    payload = answer.model_dump(exclude={"content_hash", "artifact_id", "request_id"})
    return canonical_json_hash(payload)


class NarrativePolisher:
    """单次、限时、可回退的回答润色。"""

    def __init__(self, llm=None) -> None:
        self._llm = llm or get_llm_client()

    def polish(self, answer, timeout_seconds: float = 2.0, trace=None):
        """润色回答；失败/超时/非 JSON 回退原回答（不改变请求成功状态）。

        trace（可选 PerfTrace）在真实调用模型时记录一次模型调用，避免
        性能报告漏记润色模型。
        """
        draft = {
            "conclusion": answer.content.conclusion,
            "menu_summary": answer.content.menu_summary,
            "reasoning_summary": answer.content.reasoning_summary,
        }
        try:
            response = self._llm.invoke(
                "answer_generation",
                _POLISH_SYSTEM_PROMPT,
                json.dumps(draft, ensure_ascii=False),
                response_format={"type": "json_object"},
                timeout_seconds=timeout_seconds,
            )
            if trace is not None:
                usage = response.get("usage") or {}
                trace.add_model_call("answer_generation", usage.get("model", "polish"),
                                     usage.get("elapsed_ms", 0))
        except Exception:
            return answer

        data = _parse_json(response.get("content", ""))
        if data is None:
            return answer

        # 输出审计：类型/禁止字段/菜名不变，任一违反回退原回答
        if not _validate_polish_fields(answer, data):
            return answer

        # 只允许三个文字字段；health_note/time_note 保持权威值不变
        content = AnswerContent(
            conclusion=data.get("conclusion") or answer.content.conclusion,
            menu_summary=data.get("menu_summary") or answer.content.menu_summary,
            reasoning_summary=data.get("reasoning_summary") or answer.content.reasoning_summary,
            health_note=answer.content.health_note,
            time_note=answer.content.time_note,
        )
        polished = answer.model_copy(update={"content": content})
        # 正文已变，重算 content_hash（保持一致性）
        return polished.model_copy(update={"content_hash": _content_hash(polished)})
