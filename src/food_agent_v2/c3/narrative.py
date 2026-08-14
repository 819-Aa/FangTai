"""C3 NarrativePolisher（可选润色，L3 Deferred Backlog）。

只润色公开解释（conclusion/menu_summary/reasoning_summary 三个文字字段），
不回改菜单身份、菜名、recipe_ids、健康结论或时间权威性。默认关闭；启用时
只在剩余预算足够（≥2.5s）时调用（最多 2.0s），失败/超时/非 JSON/校验不通过
直接回退确定性回答，不改变请求成功状态。这是一种受控的"表现层降级"。
"""

from __future__ import annotations

import json
import os

from food_agent_v2.c3.llm_client import get_llm_client
from food_agent_v2.contracts.artifacts import AnswerContent

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


class NarrativePolisher:
    """单次、限时、可回退的回答润色。"""

    def __init__(self, llm=None) -> None:
        self._llm = llm or get_llm_client()

    def polish(self, answer, timeout_seconds: float = 2.0):
        """润色回答；失败/超时/非 JSON 回退原回答（不改变请求成功状态）。"""
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
        except Exception:
            return answer

        data = _parse_json(response.get("content", ""))
        if data is None:
            return answer

        # 只允许三个文字字段；health_note/time_note 保持权威值不变
        content = AnswerContent(
            conclusion=data.get("conclusion") or answer.content.conclusion,
            menu_summary=data.get("menu_summary") or answer.content.menu_summary,
            reasoning_summary=data.get("reasoning_summary") or answer.content.reasoning_summary,
            health_note=answer.content.health_note,
            time_note=answer.content.time_note,
        )
        return answer.model_copy(update={"content": content})
