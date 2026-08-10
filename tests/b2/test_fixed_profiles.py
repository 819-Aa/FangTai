"""T10 固定 50 份档案全校验与 fail-closed 语义测试。

50 份档案逐份 valid/invalid；派生约束全部使用封闭代码；未知映射 fail-closed；
备孕等已知无批准代码阶段不产生硬约束；临时禁忌绑定标准 ingredient_id；
角色投影只含匿名 participant_ref。
"""

import pytest

from food_agent_v2.b2 import ConstraintScope, HealthProfileError, UserHealthProfileService
from food_agent_v2.b2.constraint_registry import ALLOWED_CONSTRAINT_CODES


@pytest.fixture(scope="module")
def svc() -> UserHealthProfileService:
    service = UserHealthProfileService()
    service.load()
    return service


class TestFixedProfiles:
    def test_loads_all_50(self, svc) -> None:
        assert len(svc._users) == 50

    def test_validate_all_50_valid(self, svc) -> None:
        report = svc.validate_all()
        assert report["profile_count"] == 50
        assert report["invalid_count"] == 0
        assert report["valid_count"] == 50

    def test_all_derived_constraints_use_closed_codes(self, svc) -> None:
        for user_id in range(1, 51):
            cs = svc.derive_constraints(user_id, f"p{user_id}")
            for constraint in cs.hard_constraints:
                code = getattr(constraint, "constraint_code", None)
                if code:
                    assert code in ALLOWED_CONSTRAINT_CODES

    def test_preparing_pregnancy_is_unresolved_not_hard(self, svc) -> None:
        for user_id in range(1, 51):
            user = svc.get_user(user_id)
            stages = user.get("special_group") if user else None
            if not isinstance(stages, list):
                stages = [stages] if stages else []
            if "备孕" in stages:
                cs = svc.derive_constraints(user_id, f"p{user_id}")
                unresolved = [
                    s for s in cs.unresolved_signals
                    if s.get("kind") == "special_stage_without_approved_code"
                ]
                assert any(s.get("stage") == "备孕" for s in unresolved)
                assert not any(
                    getattr(c, "constraint_code", "") and "备孕" in getattr(c, "constraint_code", "")
                    for c in cs.hard_constraints
                )
                return
        raise AssertionError("未在固定档案中找到备孕用户")

    def test_unknown_allergy_fails_closed(self) -> None:
        service = UserHealthProfileService()
        service._users = {
            999: {
                "user_id": 999,
                "gender": "男",
                "age": 30,
                "allergies": ["某种未知过敏"],
                "diseases": [],
                "special_group": None,
            }
        }
        with pytest.raises(HealthProfileError) as excinfo:
            service.derive_constraints(999, "px")
        assert excinfo.value.code == "HEALTH_PROFILE_DATA_INVALID"

    def test_validate_profile_reports_unmapped_allergy(self, svc) -> None:
        service = UserHealthProfileService()
        service._users = {
            998: {
                "user_id": 998,
                "gender": "男",
                "age": 30,
                "allergies": ["某种未知过敏"],
                "diseases": [],
                "special_group": None,
            }
        }
        result = service.validate_profile(998)
        assert result.valid is False
        assert any("未映射到封闭代码" in error for error in result.errors)


class TestTemporaryAndProjection:
    def test_temporary_taboo_binds_ingredient_id(self, svc) -> None:
        service = UserHealthProfileService()
        service._users = {1: svc.get_user(1)}
        resolver = lambda name: 42 if name == "花生" else None  # noqa: E731
        temporary = service.validate_temporary_signal(
            {"type": "taboo", "value": "花生"}, "p1", ingredient_resolver=resolver
        )
        assert temporary.taboo_ingredient_id == 42
        assert temporary.constraint_code is None
        assert temporary.scope == ConstraintScope.TURN

    def test_taboo_without_resolver_is_ambiguous(self, svc) -> None:
        service = UserHealthProfileService()
        service._users = {1: svc.get_user(1)}
        with pytest.raises(HealthProfileError) as excinfo:
            service.validate_temporary_signal({"type": "taboo", "value": "花生"}, "p1")
        assert excinfo.value.code == "HEALTH_SIGNAL_AMBIGUOUS"

    def test_taboo_with_real_b3_resolver_port(self, svc) -> None:
        # 用真实 B3 IngredientIdentityResolver 端口解析明确禁忌（返回 identity 对象）。
        from food_agent_v2.b3.identity_resolver import get_resolver

        service = UserHealthProfileService()
        service._users = {1: svc.get_user(1)}
        resolver = get_resolver()
        identity = resolver.resolve("花生")
        assert identity.identity == "resolved"

        temporary = service.validate_temporary_signal(
            {"type": "taboo", "value": "花生"}, "p1", ingredient_resolver=resolver
        )
        assert temporary.taboo_ingredient_id == identity.ingredient_id
        assert temporary.constraint_code is None

    def test_project_anonymous(self, svc) -> None:
        cs = svc.derive_constraints(1, "p1")
        model_view = svc.project_health_context("model_view", [cs])
        assert model_view[0]["participant_ref"] == "p1"
        assert "user_id" not in model_view[0]
        assert "constraint_codes" in model_view[0]

        query_view = svc.project_health_context("query_view", [cs])
        assert "constraint_codes" not in query_view[0]

    def test_project_unknown_role_fails(self, svc) -> None:
        cs = svc.derive_constraints(1, "p1")
        with pytest.raises(HealthProfileError) as excinfo:
            svc.project_health_context("secret_view", [cs])
        assert excinfo.value.code == "HEALTH_CONTEXT_PROJECTION_FAILED"
