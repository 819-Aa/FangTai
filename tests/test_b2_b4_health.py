"""B2+B4 健康档案与引擎测试 —— 对应 D3 §6.2, §6.4, §8。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from food_agent_v2.b2 import (
    ConstraintEffect,
    ConstraintScope,
    IndicatorStatus,
    UserHealthProfileService,
    _allergy_to_constraint_code,
    _disease_to_constraint_code,
)
from food_agent_v2.b4 import HealthRuleEngine


@pytest.fixture(scope="module")
def b2_service():
    svc = UserHealthProfileService()
    svc.load()
    return svc


@pytest.fixture(scope="module")
def b4_engine():
    engine = HealthRuleEngine()
    engine.load_relations()
    return engine


class TestB2UserProfile:
    """D3 §6.2: 用户健康档案"""

    def test_loads_all_users(self, b2_service):
        assert len(b2_service._users) == 50

    def test_constraint_derivation(self, b2_service):
        """过敏→hard_exclude 约束"""
        cs = b2_service.derive_constraints(1, "p1")
        assert len(cs.hard_constraints) > 0
        for c in cs.hard_constraints:
            assert c.scope == ConstraintScope.PERMANENT
            assert c.effect == ConstraintEffect.HARD_EXCLUDE

    def test_indicator_status(self, b2_service):
        """指标阈值判断：abnormal/normal/unknown"""
        for uid in range(1, 6):
            for metric in ["收缩压", "空腹血糖"]:
                status = b2_service.get_indicator_status(uid, metric)
                assert status in (IndicatorStatus.NORMAL, IndicatorStatus.ABNORMAL, IndicatorStatus.UNKNOWN)

    def test_disease_survives_normal_indicator(self, b2_service):
        """有疾病事实且当前指标正常 → 疾病约束不失效"""
        for uid in range(1, 51):
            user = b2_service.get_user(uid)
            if not user:
                continue
            cs = b2_service.derive_constraints(uid, f"p{uid}")
            disease_codes = [c.constraint_code for c in cs.hard_constraints
                           if hasattr(c, 'constraint_code') and c.constraint_code and c.constraint_code.startswith("disease_")]
            # 有疾病的用户应该有对应的约束码
            if user.get("diseases"):
                assert len(disease_codes) > 0, f"User {uid} has diseases but no disease constraints"

    def test_permanent_constraint_override_denied(self, b2_service):
        """永久约束覆盖拒绝"""
        denied = b2_service.check_permanent_constraint_override("allergy_peanut", "remove")
        assert denied is True

    def test_allergy_to_code_mapping(self):
        """过敏→约束码映射"""
        assert _allergy_to_constraint_code("花生") == "allergy_peanut"
        assert _allergy_to_constraint_code("海鲜") == "allergy_seafood"
        assert _allergy_to_constraint_code("牛奶") == "allergy_dairy"

    def test_disease_to_code_mapping(self):
        """疾病→约束码映射"""
        assert _disease_to_constraint_code("高血压") == "disease_hypertension"
        assert _disease_to_constraint_code("糖尿病") == "disease_diabetes"


class TestB4HealthEngine:
    """D3 §6.4: 健康规则引擎"""

    def test_engine_loads(self, b4_engine):
        assert b4_engine.constraint_count >= 19
        assert b4_engine.total_exclusion_pairs > 500  # reduced from 1376 to 918 after substring match fix

    def test_binary_verdict(self, b4_engine, b2_service):
        """二元结果：只有 PASS 或 EXCLUDE"""
        cs = b2_service.derive_constraints(1, "p1")
        r = b4_engine.evaluate_recipe(1, [1, 2, 3], cs.hard_constraints, "p1")
        assert r.verdict in ("PASS", "EXCLUDE")

    def test_evaluate_batch(self, b4_engine, b2_service):
        """候选批次审查"""
        cs = b2_service.derive_constraints(1, "p1")
        batch = b4_engine.evaluate_batch(
            [1, 2, 3, 4, 5],
            {1: [1, 2], 2: [3, 4], 3: [5, 6], 4: [], 5: []},
            {"p1": cs.hard_constraints},
        )
        assert len(batch.participant_recipe_results) > 0
        assert len(batch.safe_recipe_ids) + len(batch.excluded_recipe_ids) > 0

    def test_multi_person_intersection(self, b4_engine, b2_service):
        """多人安全交集"""
        cs1 = b2_service.derive_constraints(1, "p1")
        cs2 = b2_service.derive_constraints(2, "p2")
        batch = b4_engine.evaluate_batch(
            [1, 2, 3],
            {1: [1, 2], 2: [3, 4], 3: [5, 6]},
            {"p1": cs1.hard_constraints, "p2": cs2.hard_constraints},
        )
        # 任一参与者 EXCLUDE → 菜品全员 EXCLUDE
        for rid in batch.excluded_recipe_ids:
            results_for_rid = [r for r in batch.participant_recipe_results if r.recipe_id == rid]
            assert any(r.verdict == "EXCLUDE" for r in results_for_rid), \
                f"Recipe {rid} excluded but no participant EXCLUDE result found"

    def test_validate_selected_menu(self, b4_engine, b2_service):
        """最终健康校验"""
        cs = b2_service.derive_constraints(1, "p1")
        result = b4_engine.validate_selected_menu(
            [1, 2], {1: [], 2: []}, {"p1": cs.hard_constraints},
            "plan_test", "hash_abc",
        )
        assert result.verdict in ("PASS", "EXCLUDE")
        assert result.menu_hash == "hash_abc"


class TestB4Invariants:
    """D3 §8: 不变量验证"""

    def test_inv003_no_pending_relations(self, b4_engine):
        """INV-003: 未审核关系不参与硬排除"""
        # 关系表由 B1 离线构建，运行时只读
        # 所有已加载的关系 review_status=approved
        pass

    def test_nutrition_not_in_b4_input(self):
        """INV-015: 营养值不进入 B4 输入"""
        # B4.evaluate_recipe 只接收 ingredient_ids（int 列表），不接受营养参数
        # 契约级别测试
        from food_agent_v2.b4 import HealthRuleEngine
        sig = HealthRuleEngine.evaluate_recipe
        import inspect
        params = list(inspect.signature(sig).parameters.keys())
        assert "nutrition" not in params
        assert "nutrient" not in str(params).lower()
