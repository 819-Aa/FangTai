"""T12 B4 最终健康复核测试。

validate_selected_menu 重新执行同一评估核心并绑定 menu_hash（INV-001）；
不完整输入不产生业务结论。
"""

import pytest

from food_agent_v2.b2 import CodedHealthConstraint, ConstraintScope
from food_agent_v2.b4.engine import (
    HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE,
    HealthRuleEngine,
)

BUILD = "build-b4"

_RELATIONS = {"allergy_peanut": {277, 5}}
_COVERAGE = {
    "allergy_peanut": {
        "status": "complete",
        "covered_ingredient_ids": [1, 2, 3, 4, 5, 6, 277],
        "relation_count": 2,
        "reviewed_ingredient_count": 7,
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


class TestFinalRevalidation:
    def test_all_pass_menu_passes_with_hash(self, engine) -> None:
        result = engine.validate_selected_menu(
            [1, 2],
            {1: [1, 2], 2: [3, 4]},
            {"p1": [peanut_constraint()]},
            "plan_1",
            "hash_abc",
        )
        assert result.verdict == "PASS"
        assert result.menu_hash == "hash_abc"
        assert result.plan_id == "plan_1"

    def test_excluded_dish_menu_excludes(self, engine) -> None:
        result = engine.validate_selected_menu(
            [1, 7],
            {1: [1, 2], 7: [5, 6]},  # recipe 7 含被排除食材 5
            {"p1": [peanut_constraint()]},
            "plan_2",
            "hash_def",
        )
        assert result.verdict == "EXCLUDE"
        assert result.menu_hash == "hash_def"

    def test_unknown_menu_recipe_fails_closed(self, engine) -> None:
        # 配方缺失某道菜（recipe_ingredient_map 不含 8）——评估仍按给定食材执行；
        # 这里用未覆盖食材触发 fail-closed，证明复核不产出伪 PASS。
        with pytest.raises(HEALTH_CONSTRAINT_COVERAGE_INCOMPLETE):
            engine.validate_selected_menu(
                [8],
                {8: [999]},  # 999 未覆盖
                {"p1": [peanut_constraint()]},
                "plan_3",
                "hash_ghi",
            )
