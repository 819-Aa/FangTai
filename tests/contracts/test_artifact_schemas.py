"""T03 七类在线 Artifact 严格 Schema 测试。

关键语义：未知 verdict 一律拒绝；`unknown` 严格时间不得当 truthy；答案菜单
散列必须与最终校验一致（INV-005）；全部 Artifact 禁止额外字段；所有散列为
显式 64 位十六进制字段。
"""

from uuid import UUID

import pytest
from pydantic import ValidationError

from food_agent_v2.contracts import build as build_contract
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

AID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
RID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
H1 = "1" * 64
H2 = "2" * 64


def test_fixed_artifact_catalog_is_closed_at_twenty_names() -> None:
    expected = (
        "recipe_source_rows",
        "recipe_classifications",
        "user_profiles",
        "ingredient_occurrences",
        "ingredient_registry",
        "ingredient_aliases",
        "ingredient_forms",
        "ingredient_crosswalk",
        "recipe_ingredient_relations",
        "recipe_health_views",
        "recipe_step_binding_views",
        "recipe_nutrition_input_views",
        "recipe_retrieval_build_views",
        "step_tasks",
        "nutrition_features",
        "rag_documents",
        "health_relation_decisions",
        "health_relations",
        "health_relation_coverage",
        "recipe_dependencies",
    )

    assert build_contract.FIXED_ARTIFACT_NAMES == expected
    assert len(build_contract.FIXED_ARTIFACT_NAMES) == 20
    assert len(set(build_contract.FIXED_ARTIFACT_NAMES)) == 20
    assert build_contract.FIXED_RECIPE_DEPENDENCY_COUNT == 167
    assert build_contract.FIXED_RECIPE_BUNDLE_CONTAINS_COUNT == 12


def query_plan(**overrides) -> QueryPlanArtifact:
    data = {
        "artifact_id": AID,
        "request_id": RID,
        "participant_refs": ("p1",),
        "rewritten_query": "清淡晚餐",
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
        "estimated_time_feasible": True,
        "estimated_makespan_seconds": 1800,
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


class TestV2QueryAndTimeContracts:
    def test_query_plan_carries_closed_retrieval_facets(self) -> None:
        plan = query_plan(
            rewritten_query="老人 清淡 晚餐",
            meal_types=("晚餐",),
            population_tags=("老人",),
            dish_types=("热菜",),
            taste_tags=("清淡",),
            cuisine_tags=("家常",),
            scenario_tags=("日常",),
            include_ingredients=("豆腐",),
            exclude_ingredients=("辣椒",),
            nutrition_goal_codes=("low_sodium",),
        )

        assert plan.schema_version == "2.0.0"
        assert plan.meal_types == ("晚餐",)
        assert plan.population_tags == ("老人",)
        assert plan.exclude_ingredients == ("辣椒",)

    def test_estimated_time_is_a_boolean_with_one_makespan(self) -> None:
        menu = feasible_menu(
            estimated_time_feasible=False,
            estimated_makespan_seconds=2700,
        )

        assert menu.estimated_time_feasible is False
        assert menu.estimated_makespan_seconds == 2700

    def test_unknown_estimated_time_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            feasible_menu(estimated_time_feasible="unknown")

    def test_legacy_query_input_is_excluded_from_v2_output(self) -> None:
        legacy = QueryPlanArtifact(
            artifact_id=AID,
            request_id=RID,
            participant_refs=("p1",),
            flavor_preferences=("清淡",),
            preference_exclusions=("辣",),
            input_fingerprint=H1,
            content_hash=H2,
        )

        dumped = legacy.model_dump()
        assert dumped["schema_version"] == "2.0.0"
        assert dumped["rewritten_query"] == ""
        assert "flavor_preferences" not in dumped
        assert "preference_exclusions" not in dumped

    def test_legacy_time_input_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FeasibleMenu(
                plan_id="plan_1",
                recipe_ids=(1, 2),
                menu_hash=H1,
                score_decomposition=score_decomp(),
                strict_time_feasible=True,
                makespan_seconds=1800,
            )


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
