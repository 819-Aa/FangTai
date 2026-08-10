"""T17 Artifact 接地测试（INV-005）与全链路契约。

- 模型输出必须按 RolePolicy 类型严格 model_validate（缺失/额外/普通文本/任意 dict → SCHEMA_VALIDATION_FAILED）；
- 唯一权威 menu_hash = C2 menu_hash_for(plan_id, recipe_ids)，C3/B4/Answer/SSE 全复用；
- health_menu_planning 从工具回执分别构建真实 HealthEvaluationArtifact 与 FeasibleMenuArtifact；
- Artifact 引用生命周期：MenuDecisionArtifact 引用 B4 FinalValidationArtifact 与 FeasibleMenuArtifact 实际 id；
- 每个角色提示词规定的输出必须通过对应 model_validate。
"""

from uuid import UUID, uuid4

import pytest

from food_agent_v2.c2.schemas import menu_hash_for
from food_agent_v2.c3 import ROLE_POLICIES
from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.state import WorkflowState
from food_agent_v2.c3.tool_handler import ToolContext
from food_agent_v2.contracts.artifacts import (
    AnswerArtifact,
    FeasibleMenuArtifact,
    FinalValidationArtifact,
    HealthEvaluationArtifact,
    MenuDecisionArtifact,
    QueryPlanArtifact,
    ReviewArtifact,
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
        "final_validation_ref": "44444444-4444-4444-4444-444444444444",
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


def make_feasible(**overrides) -> FeasibleMenuArtifact:
    data = {
        "artifact_id": UUID("66666666-6666-6666-6666-666666666666"),
        "request_id": RID,
        "safe_recipe_ids_ref": "health:1",
        "menus": (),
        "input_fingerprint": "1" * 64,
        "content_hash": "2" * 64,
    }
    data.update(overrides)
    return FeasibleMenuArtifact(**data)


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
        assert WorkflowRunner._menu_hash("p1", [3, 1, 2]) == WorkflowRunner._menu_hash("p1", [1, 2, 3])

    def test_menu_hash_is_64_hex(self) -> None:
        h = WorkflowRunner._menu_hash("p1", [101, 202])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_menu_hash_is_c2_canonical(self) -> None:
        assert WorkflowRunner._menu_hash("p1", [1, 2]) == menu_hash_for("p1", [1, 2])


class TestCrossModuleMenuHashContract:
    """唯一权威 menu_hash：C2/B4/C3/FinalValidation/Answer 完全一致。"""

    def test_c2_canonical_definition(self) -> None:
        from food_agent_v2.contracts.build import canonical_json_hash

        plan_id, ids = "plan-A", [3, 1, 2]
        assert menu_hash_for(plan_id, ids) == canonical_json_hash(
            {"plan_id": plan_id, "recipe_ids": [1, 2, 3]})

    def test_runner_reuses_c2_hash(self) -> None:
        plan_id, ids = "plan-A", [101, 202]
        h = menu_hash_for(plan_id, ids)
        assert WorkflowRunner._menu_hash(plan_id, ids) == h
        assert WorkflowRunner._build_answer_base(
            [101, 202], plan_id, ToolContext(request_id=str(RID), build_id=BID),
            make_decision(plan_id=plan_id, menu_hash=h), make_final(menu_hash=h))["menu_hash"] == h

    def test_final_and_answer_carry_same_hash(self) -> None:
        plan_id = "plan-A"
        h = menu_hash_for(plan_id, [101, 202])
        final = make_final(menu_hash=h, plan_id=plan_id)
        answer = make_answer(menu_hash=h, plan_id=plan_id)
        decision = make_decision(menu_hash=h, plan_id=plan_id)
        assert final.menu_hash == answer.menu_hash == decision.menu_hash == h


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


class TestPromptContract:
    """每个角色提示词规定的语义输出，经 workflow 组装后必须通过对应 model_validate。"""

    @pytest.fixture
    def runner(self):
        class _Stub:
            def invoke(self, *a, **k):
                raise AssertionError("不应调用 LLM")
        return WorkflowRunner(build_id=BID, llm=_Stub())

    def _assemble(self, runner, payload, role):
        return runner._assemble_artifact(payload, ROLE_POLICIES[role], str(RID), ["p1"])

    def test_all_prompts_declare_strict_contract(self) -> None:
        import json
        from pathlib import Path

        data = json.loads(
            Path("config/prompts.json").resolve().read_text(encoding="utf-8"))
        for role in ("query_understanding", "menu_decision",
                     "answer_generation", "unified_review"):
            assert "严格" in data[role]["system"], role
        # health_menu_planning 收敛契约：双 Artifact 由工具回执构建，模型不伪造
        assert "由工作流按工具回执构建" in data["health_menu_planning"]["system"]

    def test_query_understanding_output_validates(self, runner) -> None:
        # 语义输出（workflow 补充 id/refs/hash）
        payload = {"dish_count_requested": 4, "flavor_preferences": ["清淡"],
                   "time_constraint_seconds": 2700, "time_constraint_policy": "hard"}
        art, err = self._assemble(runner, payload, "query_understanding")
        assert err is None
        assert isinstance(art, QueryPlanArtifact)
        assert art.dish_count_requested == 4
        assert str(art.request_id) == str(RID)
        assert art.participant_refs == ("p1",)

    def test_menu_decision_output_validates(self, runner) -> None:
        payload = {
            "plan_id": "plan-A", "recipe_ids": [101, 202], "menu_hash": "a" * 64,
            "feasible_menu_artifact_ref": "feasible:A",
            "final_validation_ref": "44444444-4444-4444-4444-444444444444",
        }
        art, err = self._assemble(runner, payload, "menu_decision")
        assert err is None
        assert isinstance(art, MenuDecisionArtifact)

    def test_answer_output_validates(self, runner) -> None:
        payload = {
            "plan_id": "plan-A", "menu_ref": "feasible:A",
            "final_validation_ref": "44444444-4444-4444-4444-444444444444",
            "recipe_ids": [101, 202], "menu_hash": "a" * 64,
            "content": {"conclusion": "推荐菜单", "menu_summary": "清蒸鱼、青菜"},
        }
        art, err = self._assemble(runner, payload, "answer_generation")
        assert err is None
        assert isinstance(art, AnswerArtifact)

    def test_review_output_validates(self, runner) -> None:
        payload = {"status": "PASS"}
        art, err = self._assemble(runner, payload, "unified_review")
        assert err is None
        assert isinstance(art, ReviewArtifact)
        assert art.status == "PASS"

    def test_review_revision_requires_target_and_issues(self, runner) -> None:
        payload = {"status": "REVISION_REQUIRED", "target_node": "answer_generation",
                   "issue_codes": ["sensitive_data"]}
        art, err = self._assemble(runner, payload, "unified_review")
        assert err is None
        assert isinstance(art, ReviewArtifact)

    def test_missing_semantic_field_fails(self, runner) -> None:
        # MenuDecisionArtifact 缺 plan_id → SCHEMA_VALIDATION_FAILED
        payload = {"recipe_ids": [1, 2]}
        _art, err = self._assemble(runner, payload, "menu_decision")
        assert err is not None
        assert err.error_code == "SCHEMA_VALIDATION_FAILED"

    def test_extra_field_fails(self, runner) -> None:
        payload = {"status": "PASS", "needs_clarification": False}
        _art, err = self._assemble(runner, payload, "unified_review")
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

    def test_empty_recipe_ids_fails(self) -> None:
        err = WorkflowRunner._answer_binding_error(
            make_answer(recipe_ids=()), make_decision(), make_final())
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


class TestDecisionRefsLifecycle:
    def test_valid_refs_pass(self) -> None:
        fv = make_final()
        feasible = make_feasible()
        md = make_decision(final_validation_ref=str(fv.artifact_id),
                           feasible_menu_artifact_ref=str(feasible.artifact_id))
        assert WorkflowRunner._decision_refs_error(md, fv, feasible) is None

    def test_wrong_final_validation_ref_fails(self) -> None:
        fv = make_final()
        feasible = make_feasible()
        md = make_decision(final_validation_ref="wrong",
                           feasible_menu_artifact_ref=str(feasible.artifact_id))
        err = WorkflowRunner._decision_refs_error(md, fv, feasible)
        assert err is not None
        assert err.error_code == "ARTIFACT_INTEGRITY_FAILED"

    def test_wrong_feasible_ref_fails(self) -> None:
        fv = make_final()
        feasible = make_feasible()
        md = make_decision(final_validation_ref=str(fv.artifact_id),
                           feasible_menu_artifact_ref="wrong")
        err = WorkflowRunner._decision_refs_error(md, fv, feasible)
        assert err is not None
        assert err.error_code == "ARTIFACT_INTEGRITY_FAILED"

    def test_missing_final_fails(self) -> None:
        feasible = make_feasible()
        md = make_decision(feasible_menu_artifact_ref=str(feasible.artifact_id))
        err = WorkflowRunner._decision_refs_error(md, None, feasible)
        assert err is not None
        assert err.error_code == "ARTIFACT_INTEGRITY_FAILED"


class TestDualArtifact:
    """health_menu_planning 分别构建真实 HealthEvaluationArtifact 与 FeasibleMenuArtifact。"""

    @pytest.fixture
    def runner(self):
        class _Stub:
            def invoke(self, *a, **k):
                raise AssertionError("不应调用 LLM")
        return WorkflowRunner(build_id=BID, llm=_Stub())

    def _receipt(self):
        from food_agent_v2.b4.schemas import HealthEvaluationReceipt, RecipeHealthResult

        return HealthEvaluationReceipt(
            evaluation_id="e1", request_id=str(RID), retrieval_result_ref=None,
            constraint_set_refs=["c1"],
            participant_recipe_results=[
                RecipeHealthResult(recipe_id=1, participant_ref="p1", verdict="PASS"),
                RecipeHealthResult(recipe_id=2, participant_ref="p1", verdict="PASS"),
            ],
            safe_recipe_ids=[1, 2], excluded_recipe_ids=[3],
            input_fingerprint="fp", evidence_refs=["ev1"],
        )

    def _plans(self, plan_recipe_ids=(1, 2)):
        from food_agent_v2.c2.schemas import FeasibleMenu

        plan_id = "plan-A"
        return [
            FeasibleMenu(plan_id=plan_id, recipe_ids=list(plan_recipe_ids),
                         menu_hash=menu_hash_for(plan_id, list(plan_recipe_ids)),
                         dominant_objective="balanced",
                         total_score=0.8, time_score=0.1, nutrition_score=0.2,
                         preference_score=0.3, diversity_score=0.2,
                         makespan_seconds=1800, strict_time_feasible=True),
        ]

    def test_builds_both_real_artifacts(self, runner) -> None:
        state = WorkflowState(request_id=str(RID), build_id=BID, participant_refs=["p1"])
        ctx = ToolContext(request_id=str(RID), build_id=BID)
        ctx.previous_results["health_evaluation"] = self._receipt()
        ctx.previous_results["feasible_menus"] = self._plans()
        new_state, feasible = runner._build_dual_artifacts(state, ctx)
        assert new_state.is_terminal() is False
        assert isinstance(new_state.health_evaluation_artifact, HealthEvaluationArtifact)
        assert isinstance(new_state.feasible_menu_artifact, FeasibleMenuArtifact)
        assert isinstance(feasible, FeasibleMenuArtifact)
        assert set(new_state.health_evaluation_artifact.safe_recipe_ids) == {1, 2}
        for m in feasible.menus:
            assert set(m.recipe_ids).issubset(
                set(new_state.health_evaluation_artifact.safe_recipe_ids))
            assert m.menu_hash == menu_hash_for(m.plan_id, list(m.recipe_ids))

    def test_plan_with_unsafe_dish_fails(self, runner) -> None:
        state = WorkflowState(request_id=str(RID), build_id=BID, participant_refs=["p1"])
        ctx = ToolContext(request_id=str(RID), build_id=BID)
        ctx.previous_results["health_evaluation"] = self._receipt()
        ctx.previous_results["feasible_menus"] = self._plans(plan_recipe_ids=(1, 999))
        new_state, _feasible = runner._build_dual_artifacts(state, ctx)
        assert new_state.is_terminal()
        assert new_state.error is not None
        assert new_state.error.error_code == "ARTIFACT_INTEGRITY_FAILED"

    def test_missing_receipt_fails(self, runner) -> None:
        state = WorkflowState(request_id=str(RID), build_id=BID, participant_refs=["p1"])
        ctx = ToolContext(request_id=str(RID), build_id=BID)
        ctx.previous_results["feasible_menus"] = self._plans()
        new_state, _feasible = runner._build_dual_artifacts(state, ctx)
        assert new_state.is_terminal()
        assert new_state.error.error_code == "HEALTH_MENU_PLANNING_EMPTY"


class TestFinalValidationExtraction:
    def test_missing_result_returns_none(self) -> None:
        ctx = ToolContext(request_id=str(RID), build_id=BID)
        assert WorkflowRunner._extract_final_validation(ctx) is None

    def test_non_artifact_returns_none(self) -> None:
        ctx = ToolContext(request_id=str(RID), build_id=BID)
        ctx.previous_results["final_validation"] = {"verdict": "PASS"}  # dict 非权威 Artifact
        assert WorkflowRunner._extract_final_validation(ctx) is None

    def test_artifact_returned(self) -> None:
        ctx = ToolContext(request_id=str(RID), build_id=BID)
        fv = make_final()
        ctx.previous_results["final_validation"] = fv
        assert WorkflowRunner._extract_final_validation(ctx) is fv
