"""NarrativePolisher 润色测试（L3 Deferred）。"""

from __future__ import annotations

import json
import uuid

from food_agent_v2.c3.narrative import NarrativePolisher
from food_agent_v2.contracts.artifacts import AnswerArtifact, AnswerContent


class _FakeLLM:
    def __init__(self, content: str | None = None, raise_on_call: Exception | None = None):
        self.calls = 0
        self._content = content
        self.raise_on_call = raise_on_call

    def invoke(self, role, system_prompt, user_message, tools=None,
               response_format=None, timeout_seconds=None):
        self.calls += 1
        if self.raise_on_call:
            raise self.raise_on_call
        return {"content": self._content or "{}", "tool_calls": [], "usage": {}}


def _answer() -> AnswerArtifact:
    return AnswerArtifact(
        artifact_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        plan_id="p1",
        menu_ref="m1",
        final_validation_ref="f1",
        recipe_ids=(1, 2, 3),
        menu_hash="0" * 64,
        content=AnswerContent(
            conclusion="为您推荐 3 道菜",
            menu_summary="菜A、菜B、菜C",
            reasoning_summary="菜品已筛选",
            health_note="",
            time_note="",
        ),
        content_hash="0" * 64,
    )


def test_polish_updates_public_text():
    llm = _FakeLLM(content=json.dumps({
        "conclusion": "为您推荐三道家常菜",
        "menu_summary": "菜A、菜B、菜C",
        "reasoning_summary": "荤素搭配合理",
    }))
    polished = NarrativePolisher(llm).polish(_answer())
    assert llm.calls == 1
    assert polished.content.conclusion == "为您推荐三道家常菜"
    assert polished.content.reasoning_summary == "荤素搭配合理"


def test_polish_failure_falls_back_to_original():
    llm = _FakeLLM(raise_on_call=TimeoutError())
    a = _answer()
    polished = NarrativePolisher(llm).polish(a)
    assert polished is a  # 失败回退原对象


def test_polish_preserves_health_and_time_notes():
    # 只允许三个文字字段；health_note/time_note 保持权威值不变
    llm = _FakeLLM(content=json.dumps({"conclusion": "新结论"}))
    polished = NarrativePolisher(llm).polish(_answer())
    assert polished.content.health_note == ""
    assert polished.content.time_note == ""
    assert polished.content.menu_summary == "菜A、菜B、菜C"


def test_polish_rejects_changed_dish_names():
    # 模型改了菜名 → 回退原回答
    llm = _FakeLLM(content=json.dumps({
        "conclusion": "为您推荐三道菜",
        "menu_summary": "完全不同的菜、菜B、菜C",
        "reasoning_summary": "搭配合理",
    }))
    a = _answer()
    assert NarrativePolisher(llm).polish(a) is a


def test_polish_rejects_medical_conclusion():
    # 模型写了医学结论（疾病词）→ 回退
    llm = _FakeLLM(content=json.dumps({
        "conclusion": "适合糖尿病患者",
        "menu_summary": "菜A、菜B、菜C",
        "reasoning_summary": "搭配合理",
    }))
    a = _answer()
    assert NarrativePolisher(llm).polish(a) is a


def test_polish_rejects_non_string_fields():
    # 异常字段类型（整数）→ 回退，不抛 ValidationError
    llm = _FakeLLM(content=json.dumps({"conclusion": 123}))
    a = _answer()
    assert NarrativePolisher(llm).polish(a) is a


def test_polish_recomputes_content_hash():
    llm = _FakeLLM(content=json.dumps({"conclusion": "新结论"}))
    polished = NarrativePolisher(llm).polish(_answer())
    assert polished.content_hash != "0" * 64
