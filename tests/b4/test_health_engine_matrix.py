"""T12 B4 fail-closed 健康引擎矩阵测试。

PASS/EXCLUDE 只在关系全集与覆盖完整时产生；缺覆盖/未覆盖食材/禁忌缺
ingredient_id 一律抛确定性错误；只依赖固定关系，无关键词/类别/模型/营养推断。
"""

import pytest

from food_agent_v2.b2 import CodedHealthConstraint, ConstraintScope, ExplicitFoodTabooConstraint
from food_agent_v2.b4.engine import (
    HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE,
    HEALTH_INGREDIENT_SET_INCOMPLETE,
    HealthRuleEngine,
)

BUILD = "build-b4"

_RELATIONS = {
    "allergy_peanut": {277, 5},
    "disease_hypertension": {99},
}

_COVERAGE = {
    "allergy_peanut": {
        "status": "complete",
        "covered_ingredient_ids": [1, 2, 3, 4, 5, 277],
        "relation_count": 2,
        "reviewed_ingredient_count": 6,
    },
    "disease_hypertension": {
        "status": "complete",
        "covered_ingredient_ids": [1, 2, 3, 4, 5, 99],
        "relation_count": 1,
        "reviewed_ingredient_count": 6,
    },
}


class FakeHealthData:
    def ready_build_id(self) -> str:
        return BUILD

    def relations(self, build_id: str) -> dict[str, set[int]]:
        return {code: set(ids) for code, ids in _RELATIONS.items()}

    def coverage(self, build_id: str) -> dict[str, dict]:
        return {code: dict(info) for code, info in _COVERAGE.items()}


def peanut_constraint() -> CodedHealthConstraint:
    return CodedHealthConstraint(
        constraint_code="allergy_peanut",
        participant_ref="p1",
        source_refs=["source"],
        scope=ConstraintScope.PERMANENT,
    )


@pytest.fixture
def engine() -> HealthRuleEngine:
    eng = HealthRuleEngine(FakeHealthData())
    eng.load_relations()
    return eng


class TestHealthEngineMatrix:
    def test_excluded_by_hard_relation(self, engine) -> None:
        result = engine.evaluate_recipe(1, [1, 2, 277], [peanut_constraint()], "p1")
        assert result.verdict == "EXCLUDE"
        assert any(h["ingredient_id"] == 277 for h in result.hitting_constraints)

    def test_clean_recipe_passes(self, engine) -> None:
        result = engine.evaluate_recipe(1, [1, 2], [peanut_constraint()], "p1")
        assert result.verdict == "PASS"

    def test_uncovered_constraint_code_fails_closed(self) -> None:
        engine = HealthRuleEngine(FakeHealthData())
        engine.load_relations()
        unknown = CodedHealthConstraint(
            constraint_code="allergy_unknown", participant_ref="p1",
            source_refs=["s"], scope=ConstraintScope.PERMANENT,
        )
        with pytest.raises(HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE):
            engine.evaluate_recipe(1, [1], [unknown], "p1")

    def test_uncovered_ingredient_fails_closed(self) -> None:
        engine = HealthRuleEngine(FakeHealthData())
        engine.load_relations()
        with pytest.raises(HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE):
            # ingredient 999 不在 allergy_peanut 的 covered 集合中。
            engine.evaluate_recipe(1, [999], [peanut_constraint()], "p1")

    def test_taboo_without_ingredient_id_fails_closed(self, engine) -> None:
        taboo = ExplicitFoodTabooConstraint(
            taboo_ingredient_id=None,
            taboo_ingredient_name="花生",
            participant_ref="p1",
            source_refs=["s"],
        )
        with pytest.raises(HEALTH_INGREDIENT_SET_INCOMPLETE):
            engine.evaluate_recipe(1, [1], [taboo], "p1")

    def test_prebound_taboo_excludes(self, engine) -> None:
        taboo = ExplicitFoodTabooConstraint(
            taboo_ingredient_id=5,
            taboo_ingredient_name="花生",
            participant_ref="p1",
            source_refs=["s"],
        )
        result = engine.evaluate_recipe(1, [1, 5], [taboo], "p1")
        assert result.verdict == "EXCLUDE"

    def test_engine_not_loaded_fails_closed(self) -> None:
        engine = HealthRuleEngine(FakeHealthData())
        with pytest.raises(HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE):
            engine.evaluate_recipe(1, [1], [peanut_constraint()], "p1")

    def test_missing_recipe_ingredient_map_fails_closed(self, engine) -> None:
        # 批次中某道菜缺失食材集合：不能按空集评估出 PASS（R-001）。
        with pytest.raises(HEALTH_INGREDIENT_SET_INCOMPLETE):
            engine.evaluate_batch([1, 9], {1: [1, 2]}, {"p1": [peanut_constraint()]})
