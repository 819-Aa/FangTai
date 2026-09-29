"""澄清链路生命周期契约单元测试。

验证 ClarificationOption, ClarificationPublicPayload, ClarificationPrivateSnapshot,
ClarificationTransition, ClarificationView, ClarificationResponseInput 等模型的
Schema、严格校验、额外字段禁止以及序列化。
"""

import pytest
from pydantic import ValidationError

from food_agent_v2.contracts.clarification import (
    ClarificationOption,
    ClarificationOptionView,
    ClarificationPrivateSnapshot,
    ClarificationPublicPayload,
    ClarificationResponseInput,
    ClarificationTransition,
    ClarificationView,
)


def test_clarification_option_schema():
    opt = ClarificationOption(
        option_id=1,
        text="放宽时间要求",
        modifications={"time_constraint_policy": "relax"},
    )
    assert opt.option_id == 1
    assert opt.text == "放宽时间要求"
    assert opt.modifications == {"time_constraint_policy": "relax"}

    # 禁止额外字段
    with pytest.raises(ValidationError):
        ClarificationOption(option_id=1, text="测试", extra_key="illegal")


def test_clarification_public_payload():
    payload = ClarificationPublicPayload(
        question_text="未能生成符合条件的方案，请选择：",
        options=[
            ClarificationOptionView(option_id=1, text="选项1"),
            ClarificationOptionView(option_id=2, text="选项2"),
        ],
        inquiry_category="NO_FEASIBLE_MENU",
    )
    assert len(payload.options) == 2
    assert payload.inquiry_category == "NO_FEASIBLE_MENU"
    # 公开选项视图绝对不能有 modifications
    for opt in payload.options:
        assert not hasattr(opt, "modifications")

    # 严禁传入含有 modifications 的内部选项对象或字典
    with pytest.raises(ValidationError):
        ClarificationPublicPayload(
            question_text="q",
            options=[{"option_id": 1, "text": "opt", "modifications": {"dish_count": 2}}],
        )

    # 禁止额外字段
    with pytest.raises(ValidationError):
        ClarificationPublicPayload(question_text="q", options=[], unexpected="val")


def test_clarification_private_snapshot():
    snap = ClarificationPrivateSnapshot(
        query_plan_snapshot={"dish_count_requested": 3},
        option_modifications={
            1: {"time_constraint_policy": "relax"},
            2: {"dish_count_requested": 2},
        },
        current_menu_version="plan-1",
        evidence_refs=["ev-1"],
    )
    assert snap.query_plan_snapshot["dish_count_requested"] == 3
    assert snap.option_modifications[2]["dish_count_requested"] == 2
    assert snap.current_menu_version == "plan-1"
    assert snap.evidence_refs == ["ev-1"]


def test_clarification_view_and_option_view():
    opt_view = ClarificationOptionView(option_id=1, text="选项一")
    assert opt_view.option_id == 1
    # 选项视图不应存在 modifications 字段
    assert not hasattr(opt_view, "modifications")

    view = ClarificationView(
        question_id="q-1",
        question_text="请调整需求",
        options=[opt_view],
        expires_at=1770000000.0,
        status="pending",
    )
    assert view.question_id == "q-1"
    assert view.status == "pending"


def test_clarification_response_input():
    resp = ClarificationResponseInput(question_id="q-100", option_id=2)
    assert resp.question_id == "q-100"
    assert resp.option_id == 2

    with pytest.raises(ValidationError):
        ClarificationResponseInput(question_id="q-100", option_id=2, modifications={})


def test_clarification_transition():
    trans = ClarificationTransition(
        expected_question_id="q-prev",
        selected_option_id=1,
        expected_revision=2,
        next_question_id="q-next",
        next_public_payload={"question_text": "q2", "options": [{"option_id": 1, "text": "opt1"}]},
        next_private_snapshot={"current_menu_version": "v2"},
        next_expires_at=1800000000.0,
        supersede_current=False,
    )
    assert trans.expected_question_id == "q-prev"
    assert trans.selected_option_id == 1
    assert trans.expected_revision == 2
    assert trans.next_question_id == "q-next"

    # 默认值
    empty_trans = ClarificationTransition()
    assert empty_trans.expected_question_id is None
    assert empty_trans.supersede_current is False

    # 校验：selected_option_id 未指定 expected_question_id
    with pytest.raises(ValidationError):
        ClarificationTransition(selected_option_id=1)

    # 校验：expected_question_id 未指定 selected_option_id
    with pytest.raises(ValidationError):
        ClarificationTransition(expected_question_id="q-prev")

    # 校验：supersede_current 与 selected_option_id 冲突
    with pytest.raises(ValidationError):
        ClarificationTransition(supersede_current=True, selected_option_id=1, expected_question_id="q-prev")

    # 校验：next_question_id 携带空 payload
    with pytest.raises(ValidationError):
        ClarificationTransition(next_question_id="q-next", next_public_payload={})

    # 校验：next_public_payload 携带内部 modifications
    with pytest.raises(ValidationError):
        ClarificationTransition(
            next_question_id="q-next",
            next_public_payload={
                "question_text": "q",
                "options": [{"option_id": 1, "text": "o1", "modifications": {"a": 1}}],
            },
        )
