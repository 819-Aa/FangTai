"""T03 七类在线 Artifact 严格 Schema 测试。

关键语义：未知 verdict 一律拒绝；`unknown` 严格时间不得当 truthy；答案菜单
散列必须与最终校验一致（INV-005）；全部 Artifact 禁止额外字段；所有散列为
显式 64 位十六进制字段。
"""

from uuid import UUID

import pytest
from pydantic import ValidationError

from food_agent_v2.contracts.artifacts import (
    AnswerArtifact,
    AnswerContent,
    ArtifactIntegrityError,
    FeasibleMenu,
    FeasibleMenuArtifact,
    FinalValidationArtifact,
    HealthEvaluationArtifact,
    MenuDecisionArtifact,
    MenuScoreDecomposition,
    QueryPlanArtifact,
    RecipeGroupHealthResult,
    ReviewArtifact,
    validate_answer_menu_binding,
)
from food_agent_v2.contracts.status import strict_time_is_feasible

AID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
RID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
H1 = "1" * 64
H2 = "2" * 64


def query_plan(**overrides) -> QueryPlanArtifact:
    data = {
        "artifact_id": AID,
        "request_id": RID,
        "participant_refs": ("p1",),
        "input_fingerprint": H1,
        "content_hash": H2,
    }
    data.update(overrides)
    return QueryPlanArtifact(**data)


def health_eval(**overrides) -> HealthEvaluationArtifact:
    data = {
        "artifact_id": AID,
        "request_id": RID,
        "participant_refs": ("p1",),
        "safe_recipe_ids": (1, 2),
        "excluded_recipe_ids": (3,),
        "participant_recipe_results": (),
        "recipe_group_results": (),
        "input_fingerprint": H1,
        "content_hash": H2,
    }
    data.update(overrides)
    return HealthEvaluationArtifact(**data)


def score_decomp(**overrides) -> MenuScoreDecomposition:
    data = {
        "total_score": 1.0,
        "rag_score": 0.1,
        "preference_score": 0.2,
        "nutrition_score": 0.2,
        "time_score": 0.1,
        "diversity_score": 0.1,
        "historical_score": 0.1,
    }
    data.update(overrides)
    return MenuScoreDecomposition(**data)


def feasible_menu(**overrides) -> FeasibleMenu:
    data = {
        "plan_id": "plan_1",
        "recipe_ids": (1, 2),
        "menu_hash": H1,
        "score_decomposition": score_decomp(),
        "strict_time_feasible": True,
    }
    data.update(overrides)
    return FeasibleMenu(**data)


def feasible_menu_artifact(**overrides) -> FeasibleMenuArtifact:
    data = {
        "artifact_id": AID,
        "request_id": RID,
        "safe_recipe_ids_ref": "he",
        "menus": (feasible_menu(),),
        "input_fingerprint": H1,
        "content_hash": H2,
    }
    data.update(overrides)
    return FeasibleMenuArtifact(**data)


def menu_decision(**overrides) -> MenuDecisionArtifact:
    data = {
        "artifact_id": AID,
        "request_id": RID,
        "plan_id": "plan_1",
        "recipe_ids": (1, 2),
        "menu_hash": H1,
        "feasible_menu_artifact_ref": "fm",
        "final_validation_ref": "fv",
        "participant_refs": ("p1",),
        "content_hash": H2,
    }
    data.update(overrides)
    return MenuDecisionArtifact(**data)


def final_validation(**overrides) -> FinalValidationArtifact:
    data = {
        "artifact_id": AID,
        "request_id": RID,
        "plan_id": "plan_1",
        "menu_artifact_ref": "fm",
        "participant_refs": ("p1",),
        "recipe_ids": (1, 2),
        "participant_recipe_results": (),
        "menu_hash": H1,
        "input_fingerprint": H1,
        "status": "PASS",
    }
    data.update(overrides)
    return FinalValidationArtifact(**data)


def answer(**overrides) -> AnswerArtifact:
    data = {
        "artifact_id": AID,
        "request_id": RID,
        "plan_id": "plan_1",
        "menu_ref": "md",
        "final_validation_ref": "fv",
        "recipe_ids": (1, 2),
        "menu_hash": H1,
        "content": AnswerContent(conclusion="选择方案A", menu_summary="两道菜"),
        "content_hash": H2,
    }
    data.update(overrides)
    return AnswerArtifact(**data)


def review(**overrides) -> ReviewArtifact:
    data = {
        "artifact_id": AID,
        "request_id": RID,
        "status": "PASS",
        "content_hash": H1,
    }
    data.update(overrides)
    return ReviewArtifact(**data)


class TestStrictTime:
    def test_unknown_not_truthy(self) -> None:
        assert strict_time_is_feasible("unknown") is False
        assert strict_time_is_feasible(True) is True
        assert strict_time_is_feasible(False) is False

    def test_unknown_parses_as_string_not_true(self) -> None:
        menu = feasible_menu(strict_time_feasible="unknown")
        assert menu.strict_time_feasible == "unknown"
        assert menu.strict_time_feasible is not True
        assert strict_time_is_feasible(menu.strict_time_feasible) is False


class TestVerdictRejected:
    def test_final_validation_unknown_verdict_rejected(self) -> None:
        with pytest.raises(ValidationError):
            final_validation(status="PARTIAL")

    def test_group_result_unknown_verdict_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecipeGroupHealthResult(
                recipe_id=1,
                participant_result_refs=("r1",),
                group_status="partial",
            )

    def test_review_unknown_verdict_rejected(self) -> None:
        with pytest.raises(ValidationError):
            review(status="APPROVED")

    def test_review_revision_requires_target_and_issues(self) -> None:
        with pytest.raises(ValidationError):
            review(status="REVISION_REQUIRED")
        with pytest.raises(ValidationError):
            review(status="REVISION_REQUIRED", target_node="answer_generation")
        ok = review(
            status="REVISION_REQUIRED",
            target_node="answer_generation",
            issue_codes=("missing_grounding",),
        )
        assert ok.status == "REVISION_REQUIRED"


class TestAnswerMenuBinding:
    def test_menu_hash_mismatch_rejected(self) -> None:
        decision = menu_decision(menu_hash=H1)
        final = final_validation(menu_hash=H2, status="PASS")
        answer_artifact = answer(plan_id=decision.plan_id, menu_hash=H2, recipe_ids=decision.recipe_ids)
        with pytest.raises(ArtifactIntegrityError) as excinfo:
            validate_answer_menu_binding(answer_artifact, decision, final)
        assert excinfo.value.code == "ANSWER_MENU_MISMATCH"

    def test_plan_id_mismatch_rejected(self) -> None:
        decision = menu_decision(plan_id="plan_1", menu_hash=H1)
        final = final_validation(plan_id="plan_1", menu_hash=H1, status="PASS")
        answer_artifact = answer(plan_id="plan_2", menu_hash=H1, recipe_ids=decision.recipe_ids)
        with pytest.raises(ArtifactIntegrityError) as excinfo:
            validate_answer_menu_binding(answer_artifact, decision, final)
        assert excinfo.value.code == "ANSWER_MENU_MISMATCH"

    def test_extra_recipe_rejected(self) -> None:
        decision = menu_decision(recipe_ids=(1, 2), menu_hash=H1)
        final = final_validation(menu_hash=H1, status="PASS")
        answer_artifact = answer(plan_id="plan_1", menu_hash=H1, recipe_ids=(1, 2, 99))
        with pytest.raises(ArtifactIntegrityError) as excinfo:
            validate_answer_menu_binding(answer_artifact, decision, final)
        assert excinfo.value.code == "ANSWER_MENU_MISMATCH"

    def test_matching_binding_passes(self) -> None:
        decision = menu_decision(plan_id="plan_1", recipe_ids=(1, 2), menu_hash=H1)
        final = final_validation(plan_id="plan_1", recipe_ids=(1, 2), menu_hash=H1, status="PASS")
        answer_artifact = answer(plan_id="plan_1", recipe_ids=(1, 2), menu_hash=H1)
        validate_answer_menu_binding(answer_artifact, decision, final)


class TestExtraFieldsRejected:
    @pytest.mark.parametrize(
        "constructor",
        [query_plan, health_eval, feasible_menu_artifact, menu_decision, final_validation, answer, review],
    )
    def test_artifact_extra_field_rejected(self, constructor) -> None:
        with pytest.raises(ValidationError):
            constructor(extra_field="forbidden")

    def test_answer_content_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AnswerContent(conclusion="x", menu_summary="y", extra="z")

    def test_score_decomp_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            score_decomp(extra="z")


class TestHashFormat:
    def test_short_content_hash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            query_plan(content_hash="short")

    def test_short_menu_hash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            feasible_menu(menu_hash="x" * 10)

    def test_short_input_fingerprint_rejected(self) -> None:
        with pytest.raises(ValidationError):
            final_validation(input_fingerprint="")
