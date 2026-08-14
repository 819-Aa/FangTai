"""FastIntentRouter 健康边界 fail-closed 测试（L1 Task 4）。

健康语义（过敏/疾病/指标/不能吃）不得当作普通推荐继续；口味偏好（甜/清淡）
走 preference_exclusions；相对称谓多人矛盾转 conflict；替换/否定/恢复识别。
"""

from __future__ import annotations

import pytest

from food_agent_v2.c3.fast_intent import FastIntentRouter


@pytest.mark.parametrize("text", [
    "我对花生过敏",
    "最近血压有点高",
    "二号参与者不能吃虾",
    "有糖尿病，不能吃甜的",
])
def test_unresolved_health_language_never_continues_as_plain_recommendation(text):
    result = FastIntentRouter.route(text, participant_refs=("p1", "p2"))
    assert result.intent in {"model_fallback", "needs_clarification"}
    assert result.unresolved_health_text is not None


def test_not_too_sweet_is_soft_preference():
    result = FastIntentRouter.route("别太甜", participant_refs=("p1",))
    assert result.health_exclusions == ()
    assert "甜" in result.preference_exclusions


def test_relative_multi_person_conflict_requires_clarification():
    result = FastIntentRouter.route(
        "一个人想吃辣，一个人一点辣都不想碰", participant_refs=("p1", "p2"))
    assert result.intent == "conflict"


def test_replace_reject_restore_intents():
    assert FastIntentRouter.route("把红烧肉换成清蒸鱼").intent == "replace"
    assert FastIntentRouter.route("重新推荐一批").intent == "reject_plan"
    assert FastIntentRouter.route("回到之前那个方案").intent == "restore"


def test_explicit_taboo_still_resolves():
    # 明确食材禁忌（无"不能/过敏"健康语言）仍走 health_exclusions
    result = FastIntentRouter.route("别做辣的", participant_refs=("p1",))
    assert result.intent == "new_recommendation"
    assert result.health_exclusions == ("p1:禁忌:辣椒",)
