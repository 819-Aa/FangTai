"""QueryNormalizer 单次模型归一化测试（L1 Task 5）。"""

from __future__ import annotations

import json

import pytest

from food_agent_v2.c3.query_normalizer import QueryNormalizer


class _FakeLLM:
    def __init__(self, content: str | None = None, raise_on_call: Exception | None = None):
        self.calls = 0
        self.last_tools = None
        self._content = content
        self.raise_on_call = raise_on_call

    def invoke(self, role, system_prompt, user_message, tools=None,
               response_format=None, timeout_seconds=None):
        self.calls += 1
        self.last_tools = tools
        if self.raise_on_call:
            raise self.raise_on_call
        return {"content": self._content or "{}", "tool_calls": [], "usage": {}}


def test_normalizer_uses_one_call_and_no_tools():
    llm = _FakeLLM(content=json.dumps({"intent": "replace", "target_recipe_id": 2}))
    result = QueryNormalizer(llm).normalize(
        "把清淡些的要求留着，重做主菜", ("p1",), {"recipe_ids": [1, 2, 3]})
    assert llm.calls == 1
    assert llm.last_tools is None
    assert result.intent == "replace"
    assert result.target_recipe_id == 2


@pytest.mark.parametrize("failure", [TimeoutError(), ValueError("bad schema")])
def test_normalizer_failure_becomes_clarification(failure):
    llm = _FakeLLM(raise_on_call=failure)
    result = QueryNormalizer(llm).normalize(
        "给老人换一道更容易咀嚼的菜", ("p1",), {"recipe_ids": [1, 2, 3]})
    assert result.intent == "needs_clarification"


def test_normalizer_non_json_becomes_clarification():
    llm = _FakeLLM(content="这不是 JSON")
    result = QueryNormalizer(llm).normalize("随便说点啥", ("p1",))
    assert result.intent == "needs_clarification"


def test_normalizer_invalid_intent_becomes_clarification():
    llm = _FakeLLM(content=json.dumps({"intent": "not_a_real_intent"}))
    result = QueryNormalizer(llm).normalize("随便说点啥", ("p1",))
    assert result.intent == "needs_clarification"
