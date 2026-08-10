"""T17 Artifact 接地测试（INV-005）。

- 模型输出必须按 RolePolicy 类型严格 model_validate（缺失/额外/普通文本/任意 dict → SCHEMA_VALIDATION_FAILED）；
- Answer 必须解析正式 AnswerArtifact 并通过 validate_answer_menu_binding
  （plan_id/recipe_ids/menu_hash/menu_ref/final_validation_ref/PASS）；
- 最终菜单以规范 menu_hash 接地。
"""

from uuid import UUID, uuid4

import pytest

from food_agent_v2.c3 import ROLE_POLICIES
from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.state import WorkflowState
from food_agent_v2.contracts.artifacts import (
    AnswerArtifact,
    FinalValidationArtifact,
    MenuDecisionArtifact,
    QueryPlanArtifact,
)

RID = UUID("11111111-1111-1111-1111-111111111111")
BID = "22222222-2222-2222-2222-222222222222"


def make_decision(**overrides) -> MenuDecisionArtifact:
    data = {
        "artifact_id": UUID("33333333-3333-3333-3333-333333333333"),
        "request_id": RID,
        "plan_id": "plan-A",
        "recipe_ids": (101, 202),
        "menu_hash": "a" * 64,
        "feasible_menu_artifact_ref": "feasible:A",
        "final_validation_ref": "final:1",
        "participant_refs": ("p1",),
        "content_hash": "b" * 64,
    }
    data.update(overrides)
    return MenuDecisionArtifact(**data)


def make_final(**overrides) -> FinalValidationArtifact:
    data = {
        "artifact_id": UUID("44444444-4444-4444-4444-444444444444"),
        "request_id": RID,
        "plan_id": "plan-A",
        "menu_artifact_ref": "feasible:A",
        "participant_refs": ("p1",),
        "recipe_ids": (101, 202),
        "participant_recipe_results": (),
        "menu_hash": "a" * 64,
        "input_fingerprint": "c" * 64,
        "status": "PASS",
    }
    data.update(overrides)
    return FinalValidationArtifact(**data)


def make_answer(**overrides) -> AnswerArtifact:
    data = {
        "artifact_id": UUID("55555555-5555-5555-5555-555555555555"),
        "request_id": RID,
        "plan_id": "plan-A",
        "menu_ref": "feasible:A",
        "final_validation_ref": "44444444-4444-4444-4444-444444444444",
        "recipe_ids": (101, 202),
        "menu_hash": "a" * 64,
        "content": {"conclusion": "推荐清蒸鱼、青菜。", "menu_summary": "清蒸鱼、青菜"},
        "content_hash": "d" * 64,
    }
    data.update(overrides)
    return AnswerArtifact(**data)


def valid_query_dict() -> dict:
    return {
        "artifact_id": str(uuid4()),
        "request_id": str(RID),
        "participant_refs": ["p1"],
        "input_fingerprint": "e" * 64,
        "content_hash": "f" * 64,
    }


class TestMenuHashGrounding:
    def test_menu_hash_order_independent(self) -> None:
        assert WorkflowRunner._menu_hash([3, 1, 2]) == WorkflowRunner._menu_hash([1, 2, 3])

    def test_menu_hash_is_64_hex(self) -> None:
        h = WorkflowRunner._menu_hash([101, 202])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestStrictArtifactSchema:
    """模型输出必须严格符合 RolePolicy.output_artifact_type。"""

    @pytest.fixture
    def policy(self):
        return ROLE_POLICIES["query_understanding"]

    def test_valid_artifact_passes(self, policy) -> None:
        artifact, err = WorkflowRunner._validate_artifact(valid_query_dict(), policy)
        assert err is None
        assert isinstance(artifact, QueryPlanArtifact)
        assert artifact.request_id == RID

    def test_missing_required_field_fails(self, policy) -> None:
        bad = valid_query_dict()
        del bad["input_fingerprint"]
        _artifact, err = WorkflowRunner._validate_artifact(bad, policy)
        assert err is not None
        assert err.error_code == "SCHEMA_VALIDATION_FAILED"

    def test_extra_field_fails(self, policy) -> None:
        bad = valid_query_dict()
        bad["needs_clarification"] = False  # 额外字段
        _artifact, err = WorkflowRunner._validate_artifact(bad, policy)
        assert err is not None
        assert err.error_code == "SCHEMA_VALIDATION_FAILED"

    def test_plain_text_fails(self, policy) -> None:
        _artifact, err = WorkflowRunner._validate_artifact("普通文本", policy)
        assert err is not None
        assert err.error_code == "SCHEMA_VALIDATION_FAILED"

    def test_arbitrary_dict_fails(self, policy) -> None:
        _artifact, err = WorkflowRunner._validate_artifact({"plan_id": "x"}, policy)
        assert err is not None
        assert err.error_code == "SCHEMA_VALIDATION_FAILED"

    def test_unknown_artifact_type_fails(self, policy) -> None:
        from types import SimpleNamespace

        fake_policy = SimpleNamespace(output_artifact_type="BogusArtifact")
        _artifact, err = WorkflowRunner._validate_artifact(valid_query_dict(), fake_policy)
        assert err is not None
        assert err.error_code == "SCHEMA_VALIDATION_FAILED"


class TestAnswerMenuBinding:
    def test_valid_binding_passes(self) -> None:
        err = WorkflowRunner._answer_binding_error(make_answer(), make_decision(), make_final())
        assert err is None

    def test_recipe_ids_extra_fails(self) -> None:
        err = WorkflowRunner._answer_binding_error(
            make_answer(recipe_ids=(101, 999)), make_decision(), make_final())
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"

    def test_menu_hash_mismatch_fails(self) -> None:
        err = WorkflowRunner._answer_binding_error(
            make_answer(menu_hash="1" * 64), make_decision(), make_final())
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"

    def test_final_not_pass_fails(self) -> None:
        err = WorkflowRunner._answer_binding_error(
            make_answer(), make_decision(), make_final(status="EXCLUDE"))
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"

    def test_plan_id_mismatch_fails(self) -> None:
        err = WorkflowRunner._answer_binding_error(
            make_answer(plan_id="plan-X"), make_decision(), make_final())
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"

    def test_menu_ref_mismatch_fails(self) -> None:
        err = WorkflowRunner._answer_binding_error(
            make_answer(menu_ref="other-ref"), make_decision(), make_final())
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"

    def test_final_validation_ref_mismatch_fails(self) -> None:
        err = WorkflowRunner._answer_binding_error(
            make_answer(final_validation_ref="wrong"), make_decision(), make_final())
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"

    def test_missing_artifacts_fails(self) -> None:
        err = WorkflowRunner._answer_binding_error(make_answer(), None, None)
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"

    def test_empty_recipe_ids_fails(self) -> None:
        """空 recipe_ids 不得绕过接地校验（删除空列表绕过）。"""
        err = WorkflowRunner._answer_binding_error(
            make_answer(recipe_ids=()), make_decision(), make_final())
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"


class TestBuildFinalValidation:
    def test_from_authoritative_b4_result(self) -> None:
        state = WorkflowState(request_id=str(RID), build_id=BID, participant_refs=["p1"])
        fv = {
            "verdict": "PASS",
            "plan_id": "plan-A",
            "recipe_ids": [101, 202],
            "menu_hash": "a" * 64,
            "participant_recipe_results": [
                {"recipe_id": 101, "participant_ref": "p1", "status": "PASS"},
            ],
            "evidence_refs": ["ev1"],
        }
        final = WorkflowRunner._build_final_validation_artifact(state, fv, make_decision())
        assert final.status == "PASS"
        assert final.plan_id == "plan-A"
        assert final.menu_hash == "a" * 64
        assert tuple(final.recipe_ids) == (101, 202)


class TestFinalValidationExtraction:
    def test_missing_result_returns_none(self) -> None:
        from food_agent_v2.c3.tool_handler import ToolContext

        ctx = ToolContext(request_id=str(RID), build_id=BID)
        assert WorkflowRunner._extract_final_validation(ctx) is None

    def test_missing_verdict_key_returns_none(self) -> None:
        from food_agent_v2.c3.tool_handler import ToolContext

        ctx = ToolContext(request_id=str(RID), build_id=BID)
        ctx.previous_results["final_validation"] = {"plan_id": "p"}  # 缺 verdict
        assert WorkflowRunner._extract_final_validation(ctx) is None

    def test_complete_result_returned(self) -> None:
        from food_agent_v2.c3.tool_handler import ToolContext

        ctx = ToolContext(request_id=str(RID), build_id=BID)
        fv = {"verdict": "PASS", "plan_id": "p", "recipe_ids": [1], "menu_hash": "a" * 64}
        ctx.previous_results["final_validation"] = fv
        assert WorkflowRunner._extract_final_validation(ctx) == fv
