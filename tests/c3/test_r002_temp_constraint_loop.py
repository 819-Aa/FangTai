"""R-002：会话/本轮临时健康信号闭环。

用户在本轮明确新增的过敏/禁忌/疾病信号（QueryPlanArtifact.health_exclusions）
必须经 B2.validate_temporary_signal 验证 → C4 存储 → 合并进 B4 健康审查；
验证失败（无法封闭映射）必须进入 needs_clarification，不得静默丢弃。
"""

from unittest.mock import patch

from food_agent_v2.c3.runner import WorkflowRunner, _parse_health_exclusion
from food_agent_v2.c3.state import RequestStatus
from food_agent_v2.c4 import (
    ConstraintScope,
    ContextService,
    SharedWorkflowContext,
)
from food_agent_v2.contracts.artifacts import QueryPlanArtifact

RID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SID = "sess_r002"
BUILD = "8f98393e-4ae2-4c00-bd0b-1cb07cd91a6f"


def _mem_c4() -> ContextService:
    """构造无 Redis/MySQL 持久化的内存版 ContextService（测试用）。"""
    c4 = ContextService()
    # 屏蔽 Redis 持久化与 MySQL 已提交边界访问，使 C4 行为纯内存化
    c4._persist_session = lambda ctx, token=None: None
    c4._recompute_manifest = lambda ctx: None
    c4._get_memory_source = lambda: None
    return c4


def _session() -> SharedWorkflowContext:
    """构造最小可用的 SharedWorkflowContext（C4 内存测试用）。"""
    return SharedWorkflowContext(
        request_id=RID,
        session_id=SID,
        participant_refs=["p1"],
        participant_user_id_mapping={"p1": 1},
        current_message={"raw_text": "不要香菜", "timestamp": 0.0},
    )


# ---------------------------------------------------------------------------
# 1) health_exclusion 字符串解析（三前缀格式：参与者N:类型:值）
# ---------------------------------------------------------------------------

class TestParseHealthExclusion:
    def test_allergy_format(self) -> None:
        parsed = _parse_health_exclusion("p1:过敏:花生")
        assert parsed == {"type": "allergy", "value": "花生", "participant_ref": "p1"}

    def test_taboo_format(self) -> None:
        parsed = _parse_health_exclusion("p2:禁忌:香菜")
        assert parsed == {"type": "taboo", "value": "香菜", "participant_ref": "p2"}

    def test_disease_format(self) -> None:
        parsed = _parse_health_exclusion("p1:疾病:高血压")
        assert parsed == {"type": "disease", "value": "高血压", "participant_ref": "p1"}

    def test_malformed_raises(self) -> None:
        # 缺段 / 未知类型 / 空值 → 无法解析
        for bad in ("花生", "p1:花生", "p1:未知类型:花生", "p1:过敏:"):
            try:
                _parse_health_exclusion(bad)
            except ValueError:
                continue
            raise AssertionError(f"应拒绝畸形 health_exclusion: {bad!r}")


# ---------------------------------------------------------------------------
# 2) C4 临时约束存储携带 taboo_ingredient_id，且可转换为 B4 约束对象
# ---------------------------------------------------------------------------

class TestC4TemporaryConstraint:
    def test_store_temporary_keeps_taboo_id(self) -> None:
        c4 = _mem_c4()
        c4._sessions[SID] = _session()
        cid = c4.store_temporary_constraint(SID, {
            "constraint_code": None,
            "taboo_ingredient_name": "香菜",
            "taboo_ingredient_id": 42,
            "participant_ref": "p1",
            "source_refs": ["user_signal:香菜"],
            "scope": "turn",
        })
        assert cid
        stored = c4.get_effective_constraints(SID)
        assert len(stored) == 1
        assert stored[0].taboo_ingredient_id == 42
        assert stored[0].scope == ConstraintScope.TURN

    def test_to_b4_constraints_returns_typed_objects(self) -> None:
        from food_agent_v2.b2.schemas import (
            CodedHealthConstraint,
            ExplicitFoodTabooConstraint,
        )
        c4 = _mem_c4()
        c4._sessions[SID] = _session()
        c4.store_temporary_constraint(SID, {
            "constraint_code": "allergy_peanut",
            "taboo_ingredient_name": None,
            "taboo_ingredient_id": None,
            "participant_ref": "p1",
            "source_refs": ["user_signal:花生"],
            "scope": "session",
        })
        c4.store_temporary_constraint(SID, {
            "constraint_code": None,
            "taboo_ingredient_name": "香菜",
            "taboo_ingredient_id": 42,
            "participant_ref": "p1",
            "source_refs": ["user_signal:香菜"],
            "scope": "turn",
        })
        out = c4.to_b4_constraints(SID)
        assert len(out) == 2
        coded = [c for c in out if isinstance(c, CodedHealthConstraint)]
        taboo = [c for c in out if isinstance(c, ExplicitFoodTabooConstraint)]
        assert len(coded) == 1 and coded[0].constraint_code == "allergy_peanut"
        assert len(taboo) == 1 and taboo[0].taboo_ingredient_id == 42


# ---------------------------------------------------------------------------
# 3) runner：health_exclusions 验证失败 → needs_clarification
# ---------------------------------------------------------------------------

class TestRunnerNeedsClarification:
    def test_invalid_exclusion_enters_needs_clarification(self) -> None:
        """未知过敏词无法封闭映射 → HEALTH_SIGNAL_AMBIGUOUS → needs_clarification。"""
        from food_agent_v2.c3.state import WorkflowState
        runner = WorkflowRunner()
        c4 = _mem_c4()
        state = WorkflowState(
            request_id=RID, build_id=BUILD, status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        )
        artifact = QueryPlanArtifact(
            artifact_id="10000000-0000-0000-0000-0000000000aa",
            request_id=RID,
            participant_refs=("p1",),
            health_exclusions=("p1:过敏:完全未知的过敏原XYZ",),
            input_fingerprint="1" * 64,
            content_hash="2" * 64,
        )
        with patch("food_agent_v2.c3.runner._ingredient_resolver", return_value=None):
            new_state = runner._handle_query_plan_exclusions(
                state, artifact, SID, c4, {"p1": 1})
            assert new_state is not None
            assert new_state.status == RequestStatus.NEEDS_CLARIFICATION
            assert new_state.error is not None
            assert new_state.error.error_code == "HEALTH_SIGNAL_AMBIGUOUS"

    def test_success_returns_state_not_none(self) -> None:
        """全部信号验证成功 → 返回原 state（绝不返回 None）。

        回归：R-002 初版成功路径返回 None，主循环 state=... 后立即
        state.is_terminal() 触发 'NoneType' has no attribute 'is_terminal'。
        """
        from food_agent_v2.c3.state import WorkflowState
        runner = WorkflowRunner()
        c4 = _mem_c4()
        c4._sessions[SID] = _session()
        state = WorkflowState(
            request_id=RID, build_id=BUILD, status=RequestStatus.RUNNING,
            participant_refs=["p1"],
        )
        artifact = QueryPlanArtifact(
            artifact_id="10000000-0000-0000-0000-0000000000aa",
            request_id=RID,
            participant_refs=("p1",),
            health_exclusions=("p1:疾病:高血压",),
            input_fingerprint="1" * 64,
            content_hash="2" * 64,
        )
        new_state = runner._handle_query_plan_exclusions(
            state, artifact, SID, c4, {"p1": 1})
        assert new_state is not None
        assert new_state is state  # 成功路径返回原 state
        assert new_state.status == RequestStatus.RUNNING  # 未变成澄清/失败
        assert new_state.error is None


# ---------------------------------------------------------------------------
# 4) 集成：有效临时禁忌 → C4 存储 → to_b4 含禁忌 → 不被丢弃
# ---------------------------------------------------------------------------

class TestEndToEndLoop:
    def test_valid_taboo_stored_and_convertible(self) -> None:
        """有效禁忌经验证 → C4 存储 → 可转换为 B4 约束对象（不丢 ingredient_id）。"""
        from food_agent_v2.b2 import UserHealthProfileService
        from food_agent_v2.b2.schemas import ExplicitFoodTabooConstraint
        c4 = _mem_c4()
        c4._sessions[SID] = _session()
        b2 = UserHealthProfileService()

        parsed = _parse_health_exclusion("p1:禁忌:香菜")
        # 用真实 b2 验证（需要 ingredient resolver；若 resolver 无法解析则失败）
        try:
            temp = b2.validate_temporary_signal(
                {"type": parsed["type"], "value": parsed["value"]},
                parsed["participant_ref"],
                ingredient_resolver=_fake_resolver(),
            )
        except Exception:
            temp = None
        assert temp is not None, "香菜应能解析为标准 ingredient_id"
        assert temp.taboo_ingredient_id is not None

        cid = c4.store_temporary_constraint(SID, {
            "constraint_code": temp.constraint_code,
            "taboo_ingredient_name": temp.taboo_ingredient_name,
            "taboo_ingredient_id": temp.taboo_ingredient_id,
            "participant_ref": temp.participant_ref,
            "source_refs": temp.source_refs,
            "scope": temp.scope.value,
        })
        assert cid

        b4_out = c4.to_b4_constraints(SID)
        assert len(b4_out) == 1
        assert isinstance(b4_out[0], ExplicitFoodTabooConstraint)
        assert b4_out[0].taboo_ingredient_id == temp.taboo_ingredient_id


def _fake_resolver():
    """模拟 B3 食材解析器：把香菜解析为标准 ingredient_id 42。"""
    class _Resolver:
        def resolve(self, name: str) -> int | None:
            return 42 if name == "香菜" else None
    return _Resolver()


class TestEvaluateMergesTemporaryConstraint:
    """端到端：_evaluate_recipe_health 必须把 C4 临时禁忌合并进 B4 约束。

    依赖 T23 MySQL（真实 B3 resolver + B4 关系表）；无环境时按 NOT_RUN 跳过。
    """

    def test_temp_taboo_excludes_dish(self) -> None:
        import pytest

        from food_agent_v2.c3.tool_handler import ToolContext, _evaluate_recipe_health

        try:
            from food_agent_v2.b3.identity_resolver import IngredientIdentityResolver
            resolver = IngredientIdentityResolver()
            cilantro = resolver.resolve("香菜")
        except Exception as e:
            pytest.skip(f"T23 基础设施不可用，按 NOT_RUN 跳过: {e}")

        if not (cilantro and cilantro.identity == "resolved"):
            pytest.skip("无法解析香菜 ingredient_id")

        # 用真实 B3 找一道含香菜、且无临时约束时对 user1 safe 的菜
        # （避免 user1 永久约束如海鲜过敏把候选菜排除，导致对照无效）
        from food_agent_v2.b2 import UserHealthProfileService
        from food_agent_v2.b3.recipe_views import get_view_builder
        from food_agent_v2.b4 import HealthRuleEngine
        builder = get_view_builder()
        engine = HealthRuleEngine()
        engine.load_relations()
        b2 = UserHealthProfileService()
        b2.load()
        perm = b2.derive_constraints(1, "p1").hard_constraints
        cilantro_dish = None
        for rid in range(1, 2000):
            v = builder.build_health_ingredient_view(rid)
            if v and cilantro.ingredient_id in v.ingredient_ids:
                r = engine.evaluate_batch([rid], {rid: v.ingredient_ids},
                                          {"p1": perm})
                if rid in r.safe_recipe_ids:
                    cilantro_dish = rid
                    break
        if cilantro_dish is None:
            pytest.skip("未找到含香菜且对 user1 safe 的菜")

        # 构造 C4 会话，写入临时禁忌香菜
        c4 = _mem_c4()
        c4._sessions[SID] = _session()
        c4.store_temporary_constraint(SID, {
            "constraint_code": None,
            "taboo_ingredient_name": "香菜",
            "taboo_ingredient_id": cilantro.ingredient_id,
            "participant_ref": "p1",
            "source_refs": ["user_signal:香菜"],
            "scope": "turn",
        })

        # 1) 无临时约束时评估该菜 → safe
        ctx0 = ToolContext(request_id=RID, build_id=BUILD,
                           participant_user_mapping={"p1": 1})
        res0 = _evaluate_recipe_health({"recipe_ids": [cilantro_dish]}, ctx0)
        assert cilantro_dish in res0["safe_recipe_ids"], "无临时约束时含香菜菜应为 safe"

        # 2) 带 C4 临时禁忌评估 → excluded
        ctx1 = ToolContext(request_id=RID, build_id=BUILD,
                           participant_user_mapping={"p1": 1},
                           session_id=SID, context_service=c4)
        res1 = _evaluate_recipe_health({"recipe_ids": [cilantro_dish]}, ctx1)
        assert cilantro_dish not in res1["safe_recipe_ids"], "临时禁忌香菜后应被排除"
        assert cilantro_dish in res1["excluded_recipe_ids"]
