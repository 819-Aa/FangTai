"""D2 回答与前端测试 —— 对应 D3 §6.12。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from food_agent_v2.d2 import (
    FRONTEND_DO_NOT,
    AnswerArtifact,
    UserVisibleAnalysis,
    audit_answer_text,
    map_sse_to_ui_state,
    validate_answer_dish_set,
)


class TestAnswerAudit:
    """D2 §6.12: 回答禁止表述检测"""

    def test_detect_nutrition_value(self):
        v = audit_answer_text("这道菜含钠800mg")
        assert len(v) >= 1
        assert any("nutrition_value" in item["category"] for item in v)

    def test_detect_health_judgment(self):
        v = audit_answer_text("这道菜适合高血压患者")
        assert len(v) >= 1
        assert any("health_judgment" in item["category"] for item in v)

    def test_detect_disease_indicator(self):
        v = audit_answer_text("您最近的血压偏高")
        assert len(v) >= 1
        assert any("disease_indicator" in item["category"] for item in v)

    def test_clean_text_passes(self):
        v = audit_answer_text("选择了方案A，在满足健康要求的同时更符合口味偏好。约45分钟完成。")
        assert len(v) == 0

    def test_dish_set_validation(self):
        assert validate_answer_dish_set([1, 2], [1, 2, 3]) is True
        assert validate_answer_dish_set([1, 4], [1, 2, 3]) is False
        assert validate_answer_dish_set([], [1, 2]) is True


class TestUserVisibleAnalysis:
    """D2 §12.2: 用户可见分析摘要"""

    def test_valid_analysis(self):
        a = UserVisibleAnalysis(
            stage="query_understanding",
            title="理解需求",
            summary="您需要一顿三菜一汤的午餐，偏好川菜口味。",
        )
        assert a.validate() is True

    def test_too_long_summary(self):
        a = UserVisibleAnalysis(
            stage="test", title="Test",
            summary="x" * 151,
        )
        assert a.validate() is False

    def test_forbidden_word_in_summary(self):
        for word in ["血压", "糖尿病", "毫克", "千卡"]:
            a = UserVisibleAnalysis(
                stage="test", title="Test",
                summary=f"您的{word}状态",
            )
            assert a.validate() is False, f"should reject '{word}'"


class TestAnswerArtifact:
    """D2 §5: AnswerArtifact 结构校验"""

    def test_valid_artifact(self):
        art = AnswerArtifact(
            artifact_id="a1", request_id="r1", plan_id="p1",
            menu_ref="menu_ref_1", final_validation_ref="fv_1",
            content={
                "conclusion": "为您推荐了以下菜单",
                "menu_summary": "回锅肉、麻婆豆腐、清炒时蔬、紫菜蛋花汤",
                "reasoning_summary": "方案A更符合口味偏好",
                "health_note": "部分候选因健康限制被排除",
            },
        )
        errors = art.validate()
        assert len(errors) == 0, f"unexpected errors: {errors}"

    def test_artifact_with_forbidden_content(self):
        art = AnswerArtifact(
            artifact_id="a2", request_id="r2", plan_id="p2",
            menu_ref="menu_ref_2", final_validation_ref="fv_2",
            content={
                "conclusion": "含钠800mg的推荐",
            },
        )
        errors = art.validate()
        assert len(errors) >= 1


class TestFrontendSSE:
    """D2 §8.1: 前端 SSE 状态机"""

    def test_sse_to_ui_state(self):
        assert map_sse_to_ui_state("request_accepted", "idle") == "loading"
        assert map_sse_to_ui_state("answer_ready", "loading") == "streaming_answer"
        assert map_sse_to_ui_state("result_committed", "streaming_answer") == "complete"
        assert map_sse_to_ui_state("error", "loading") == "error"
        # unknown event keeps current state
        assert map_sse_to_ui_state("unknown_event", "loading") == "loading"

    def test_frontend_do_not_list(self):
        assert len(FRONTEND_DO_NOT) >= 8
        assert any("渲染营养数值" in rule for rule in FRONTEND_DO_NOT)
        assert any("反向推断" in rule for rule in FRONTEND_DO_NOT)
