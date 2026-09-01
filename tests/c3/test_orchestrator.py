"""确定性主编排器（P4）链路测试 —— 真实 B2/B4/C2，确定性 C1/B3 fixture，FakeC4。

覆盖单轮首次推荐成功路径；多轮（有前文菜单）与 replace/reject fallback legacy。
依赖 MySQL 可用（工具执行真实领域服务）。
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from food_agent_v2.b3.repository import (
    RecipeHealthIngredientView,
    RecipeRetrievalView,
    RepositoryError,
)
from food_agent_v2.c1 import RetrievalCandidate, RetrievalResult
from food_agent_v2.c3.fast_intent import FastIntentRouter, IntentDelta
from food_agent_v2.c3.orchestrator import DeterministicRecommendationOrchestrator
from food_agent_v2.c3.query_normalizer import SemanticRewrite
from food_agent_v2.d1 import api as d1_api


def _fresh_rid() -> str:
    return str(uuid.uuid4())


def _mysql_available() -> bool:
    try:
        import pymysql

        from food_agent_v2.core.config import load_config
        cfg = load_config()
        conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                               user=cfg.mysql.user, password=cfg.mysql.password,
                               database=cfg.mysql.database, charset="utf8mb4",
                               connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


def test_semantic_rewrite_is_the_only_query_plan_and_retrieval_source() -> None:
    orchestrator = DeterministicRecommendationOrchestrator(llm=SimpleNamespace())
    routed = FastIntentRouter.route("给我推荐老人吃的晚餐", ("p1",))
    rewrite = SemanticRewrite(
        retrieval_query="老人 晚餐",
        meal_types=("晚餐",),
        population_tags=("老人",),
        exclude_ingredients=("辣椒",),
        max_time_minutes=30,
    )

    intent = orchestrator._apply_semantic_rewrite(
        routed,
        rewrite,
        ("p1",),
        has_current_menu=False,
    )
    plan = orchestrator._build_query_plan(intent, _fresh_rid(), ["p1"])

    assert plan.rewritten_query == "老人 晚餐"
    assert plan.meal_types == ("晚餐",)
    assert plan.population_tags == ("老人",)
    assert plan.exclude_ingredients == ("辣椒",)
    assert plan.time_constraint_seconds == 1800
    assert orchestrator._retrieval_query(intent) == "老人 晚餐"


def test_semantic_rewrite_preserves_and_stably_merges_routed_constraints() -> None:
    orchestrator = DeterministicRecommendationOrchestrator(llm=SimpleNamespace())
    routed = IntentDelta(
        query="三菜一汤，30分钟内，不吃花生",
        meal_types=("晚餐",),
        population_tags=("老人",),
        scenario_tags=("家庭",),
        dish_count_requested=4,
        flavor_preferences=("清淡",),
        taste_tags=("清淡",),
        cuisine_tags=("川菜",),
        dish_types=("汤",),
        include_ingredients=("豆腐",),
        exclude_ingredients=("花生",),
        nutrition_goal_codes=("low_salt",),
        health_exclusions=("p1:高血压",),
        time_constraint_seconds=1800,
        time_constraint_policy="hard",
    )
    rewrite = SemanticRewrite(
        retrieval_query="老人 晚餐 豆腐汤",
        meal_types=("晚餐", "午餐"),
        population_tags=("老人", "儿童"),
        scenario_tags=("家庭", "聚餐"),
        dish_count=2,
        taste_tags=("清淡", "鲜香"),
        cuisine_tags=("川菜", "粤菜"),
        dish_types=("汤", "素菜"),
        include_ingredients=("豆腐", "菌菇"),
        exclude_ingredients=("花生", "香菜"),
        nutrition_goal_codes=("low_salt", "high_protein"),
        health_constraints=("糖尿病",),
        max_time_minutes=45,
    )

    intent = orchestrator._apply_semantic_rewrite(
        routed,
        rewrite,
        ("p1",),
        has_current_menu=False,
    )

    assert intent.rewritten_query == "老人 晚餐 豆腐汤"
    assert intent.meal_types == ("晚餐", "午餐")
    assert intent.population_tags == ("老人", "儿童")
    assert intent.scenario_tags == ("家庭", "聚餐")
    assert intent.dish_count_requested == 4
    assert intent.flavor_preferences == ("清淡", "鲜香")
    assert intent.taste_tags == ("清淡", "鲜香")
    assert intent.cuisine_tags == ("川菜", "粤菜")
    assert intent.dish_types == ("汤", "素菜")
    assert intent.include_ingredients == ("豆腐", "菌菇")
    assert intent.exclude_ingredients == ("花生", "香菜")
    assert intent.nutrition_goal_codes == ("low_salt", "high_protein")
    assert intent.health_exclusions == ("p1:高血压",)
    assert intent.time_constraint_seconds == 1800
    assert intent.time_constraint_policy == "hard"


def test_semantic_rewrite_keeps_routed_collection_missing_from_rewrite() -> None:
    orchestrator = DeterministicRecommendationOrchestrator(llm=SimpleNamespace())
    routed = IntentDelta(
        query="晚餐要有汤",
        meal_types=("晚餐",),
        dish_types=("汤",),
        dish_count_requested=4,
    )
    rewrite = SemanticRewrite(
        retrieval_query="素菜",
        dish_types=("素菜",),
        dish_count=2,
    )

    intent = orchestrator._apply_semantic_rewrite(
        routed,
        rewrite,
        ("p1",),
        has_current_menu=False,
    )

    assert intent.meal_types == ("晚餐",)
    assert intent.dish_types == ("汤", "素菜")
    assert intent.dish_count_requested == 4


def test_semantic_rewrite_fills_empty_routed_health_exclusions() -> None:
    orchestrator = DeterministicRecommendationOrchestrator(llm=SimpleNamespace())
    rewrite = SemanticRewrite(
        retrieval_query="清淡晚餐",
        health_constraints=("糖尿病",),
    )

    intent = orchestrator._apply_semantic_rewrite(
        IntentDelta(query="糖尿病也能吃"),
        rewrite,
        ("p1",),
        has_current_menu=False,
    )

    assert intent.health_exclusions == ("p1:疾病:糖尿病",)


def test_semantic_allergy_projects_the_normalized_excluded_ingredient() -> None:
    """An allergy must use the normalized ingredient, not its raw sentence prefix."""
    orchestrator = DeterministicRecommendationOrchestrator(llm=SimpleNamespace())
    routed = FastIntentRouter.route("晚餐我花生过敏", ("p1",))
    assert routed.health_exclusions == ()
    rewrite = SemanticRewrite(
        retrieval_query="晚餐 花生",
        exclude_ingredients=("花生",),
        health_constraints=("晚餐我花生过敏",),
    )

    intent = orchestrator._apply_semantic_rewrite(
        routed, rewrite, ("p1",), has_current_menu=False
    )
    plan = orchestrator._build_query_plan(intent, _fresh_rid(), ["p1"])

    assert plan.health_exclusions == ("p1:过敏:花生",)


def test_semantic_allergy_preserves_unresolved_signal_for_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated exclusion must reach R-002 as an unparsed health signal."""
    orchestrator = DeterministicRecommendationOrchestrator(llm=SimpleNamespace())
    rewrite = SemanticRewrite(
        retrieval_query="晚餐",
        exclude_ingredients=("花生",),
        health_constraints=("我海鲜过敏",),
    )

    intent = orchestrator._apply_semantic_rewrite(
        IntentDelta(query="我海鲜过敏"), rewrite, ("p1",), has_current_menu=False
    )
    plan = orchestrator._build_query_plan(intent, _fresh_rid(), ["p1"])

    assert intent.health_exclusions == ("我海鲜过敏",)
    assert "p1:过敏:花生" not in plan.health_exclusions

    class _NoopProfileService:
        def load(self, *, expected_build_id: str) -> None:
            pass

    from food_agent_v2.c3.state import RequestStatus, WorkflowState

    monkeypatch.setattr(
        "food_agent_v2.b2.UserHealthProfileService", _NoopProfileService
    )
    monkeypatch.setattr("food_agent_v2.c3.runner._ingredient_resolver", lambda: None)
    state = WorkflowState(
        request_id=_fresh_rid(),
        build_id="test-build",
        status=RequestStatus.RUNNING,
        participant_refs=["p1"],
    )

    clarified = orchestrator._handle_query_plan_exclusions(
        state, plan, "sess_unresolved", _FakeC4(), {"p1": 1}
    )

    assert clarified.status == RequestStatus.NEEDS_CLARIFICATION
    assert clarified.error is not None
    assert clarified.error.error_code == "HEALTH_SIGNAL_AMBIGUOUS"


def test_semantic_health_signal_without_participant_fails_closed_for_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-participant signal without an owner must reach R-002 unresolved."""
    orchestrator = DeterministicRecommendationOrchestrator(llm=SimpleNamespace())
    rewrite = SemanticRewrite(
        retrieval_query="晚餐",
        health_constraints=("有人海鲜过敏",),
    )

    intent = orchestrator._apply_semantic_rewrite(
        IntentDelta(query="有人海鲜过敏"),
        rewrite,
        ("p1", "p2"),
        has_current_menu=False,
    )
    plan = orchestrator._build_query_plan(intent, _fresh_rid(), ["p1", "p2"])

    assert intent.health_exclusions == ("有人海鲜过敏",)
    assert plan.health_exclusions == ("有人海鲜过敏",)

    class _NoopProfileService:
        def load(self, *, expected_build_id: str) -> None:
            pass

    from food_agent_v2.c3.state import RequestStatus, WorkflowState

    monkeypatch.setattr(
        "food_agent_v2.b2.UserHealthProfileService", _NoopProfileService
    )
    monkeypatch.setattr("food_agent_v2.c3.runner._ingredient_resolver", lambda: None)
    state = WorkflowState(
        request_id=_fresh_rid(),
        build_id="test-build",
        status=RequestStatus.RUNNING,
        participant_refs=["p1", "p2"],
    )

    clarified = orchestrator._handle_query_plan_exclusions(
        state, plan, "sess_multi_unresolved", _FakeC4(), {"p1": 1, "p2": 2}
    )

    assert clarified.status == RequestStatus.NEEDS_CLARIFICATION
    assert clarified.error is not None
    assert clarified.error.error_code == "HEALTH_SIGNAL_AMBIGUOUS"


@pytest.mark.parametrize("constraint", ("我不能吃花生", "别吃花生"))
def test_semantic_health_taboo_projects_to_participant_constraint(constraint: str) -> None:
    orchestrator = DeterministicRecommendationOrchestrator(llm=SimpleNamespace())
    rewrite = SemanticRewrite(retrieval_query="家常菜", health_constraints=(constraint,))

    intent = orchestrator._apply_semantic_rewrite(
        IntentDelta(query=constraint), rewrite, ("p1",), has_current_menu=False
    )
    plan = orchestrator._build_query_plan(intent, _fresh_rid(), ["p1"])

    assert intent.health_exclusions == ("p1:禁忌:花生",)
    assert plan.health_exclusions == ("p1:禁忌:花生",)


pytestmark = pytest.mark.skipif(not _mysql_available(), reason="MySQL 不可用")


@pytest.fixture(autouse=True)
def _deterministic_c1_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    recipes = (
        (1, "番茄鸡蛋汤"),
        (2, "青椒肉丝"),
        (3, "清炒时蔬"),
        (5, "红烧茄子"),
        (6, "宫保鸡丁"),
    )
    from food_agent_v2.b3.recipe_views import get_view_builder

    real_builder = get_view_builder()
    health_views = {
        recipe_id: real_builder.build_health_ingredient_view(recipe_id)
        for recipe_id, _name in recipes
    }
    assert all(
        view is not None
        and view.ingredient_ids
        and view.ingredient_relations
        and view.ingredient_evidence_paths
        for view in health_views.values()
    ), "fixture recipes must retain complete B3 health views for real B4 evaluation"

    class RetrievalPort:
        def retrieve(self, _query: str, top_k: int = 20, **_kwargs):
            candidates = [
                RetrievalCandidate(
                    recipe_id=recipe_id,
                    document_id=f"fixture-{recipe_id}",
                    name=name,
                    score=float(len(recipes) - index),
                    source_paths=["test-fixture"],
                )
                for index, (recipe_id, name) in enumerate(recipes)
            ]
            return RetrievalResult(
                retrieval_id="fixture-retrieval",
                request_id=None,
                candidates=candidates[:top_k],
                total_candidates=len(candidates),
                retrieval_path="test-fixture",
                source_paths=["test-fixture"],
            )

    class FixtureViewBuilder:
        def __init__(self, health: dict[int, RecipeHealthIngredientView]) -> None:
            self._health = health
            self._retrieval = {
                recipe_id: RecipeRetrievalView(
                    recipe_id=recipe_id,
                    name=name,
                    ingredient_display_names=[],
                    ingredient_ids=[],
                    searchable_fields={"name": name},
                    step_summary=None,
                    time_reference=None,
                    ingredient_family_ids=[],
                )
                for recipe_id, name in recipes
            }

        def build_health_ingredient_view(self, recipe_id: int):
            return self._health.get(recipe_id)

        def build_retrieval_view(self, recipe_id: int):
            return self._retrieval.get(recipe_id)

        def get_recipe(self, recipe_id: int) -> dict | None:
            view = self._retrieval.get(int(recipe_id))
            if view is None:
                return None
            return {"recipe_id": view.recipe_id, "名称": view.name}

    fixture_builder = FixtureViewBuilder(health_views)

    class FixtureRepository:
        def ready_build_id(self) -> str:
            return real_builder.build_id

        def get_retrieval_view(self, recipe_ids, build_id: str):
            if build_id != self.ready_build_id():
                raise RepositoryError("BUILD_IDENTITY_MISMATCH", "fixture build mismatch")
            try:
                return [fixture_builder._retrieval[int(recipe_id)] for recipe_id in recipe_ids]
            except KeyError as exc:
                raise RepositoryError("UNKNOWN_RECIPE_ID", str(exc)) from exc

        def close(self) -> None:
            pass

    fixture_repository = FixtureRepository()

    monkeypatch.setattr(
        "food_agent_v2.c1.get_retrieval_service", lambda: RetrievalPort())
    monkeypatch.setattr(
        "food_agent_v2.b3.recipe_views.get_view_builder", lambda: fixture_builder)
    monkeypatch.setattr(
        "food_agent_v2.application.menu_projection.default_mysql_repository",
        lambda: fixture_repository,
    )


class _FakeC4:
    """确定性链路所需的 C4 替身（无前文菜单，供首次推荐 fast path）。"""

    def __init__(self, has_current_menu: bool = False,
                 query_plan: dict | None = None) -> None:
        self._sessions: dict = {}
        self._has_current_menu = has_current_menu
        self._query_plan = query_plan

    def build_shared_context(self, session_id, participant_refs, raw, mapping,
                             request_id=None, build_id=None):
        return SimpleNamespace(session_id=session_id), {}

    def validate_context_integrity(self, ref):
        return {"valid": True}

    def project_model_context(self, role, handoff, ref):
        return SimpleNamespace(role=role, conversation_visible=[],
                               constraint_visible=[], menu_visible={})

    def get_session_state(self, session_id):
        if self._has_current_menu:
            return {"session_id": session_id,
                    "current_menu": {"plan_id": "p1", "recipe_ids": [1, 2, 3]},
                    "query_plan": self._query_plan}
        return None

    def store_temporary_constraint(self, session_id, constraint):
        pass

    def to_b4_constraints(self, session_id):
        return []

    def commit_session_state(self, request_id, status, **kw):
        pass

    def _persist_session(self, ctx):
        pass


class _UnavailableLLM:
    def invoke(self, *_args, **_kwargs):
        raise RuntimeError("test LLM unavailable")


def _reset_d1(request_id: str) -> None:
    d1_api._requests.clear()
    d1_api._events.clear()
    d1_api._event_cursors.clear()
    d1_api._idempotency.clear()
    d1_api._requests[request_id] = {
        "request_id": request_id, "session_id": "sess_x", "status": "accepted",
        "created_at": "t", "updated_at": "t",
    }


def test_make_orchestrator_uses_local_llm_without_loading_configured_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deterministic factory must not initialize the configured Qwen client."""
    monkeypatch.setattr(
        "food_agent_v2.c3.runner.get_llm_client",
        lambda: pytest.fail("configured LLM client must not be loaded"),
    )

    runner = _make_orchestrator()

    assert isinstance(runner._llm, _UnavailableLLM)


def _make_orchestrator(has_current_menu: bool = False, *, llm=None,
                       query_plan: dict | None = None):
    return DeterministicRecommendationOrchestrator(
        llm=llm if llm is not None else _UnavailableLLM(),
        c4=_FakeC4(has_current_menu=has_current_menu, query_plan=query_plan),
    )


class TestDeterministicChain:
    def test_first_recommendation_completed(self) -> None:
        rid = _fresh_rid()
        _reset_d1(rid)
        runner = _make_orchestrator()
        runner.run(rid, "sess_det", "四菜一汤家常", [{"participant_ref": "p1", "user_id": "1"}])
        result = d1_api.get_request_status(rid)[1]
        assert result["status"] == "completed", f"确定性链路未 completed: {result['status']}"
        assert "番茄鸡蛋汤" in {
            item["name"] for item in result["result_summary"]["menu_summary"]["items"]
        }

    def test_dish_count_requested_respected(self) -> None:
        rid = _fresh_rid()
        _reset_d1(rid)
        runner = _make_orchestrator()
        runner.run(rid, "sess_cnt", "四菜一汤家常", [{"participant_ref": "p1", "user_id": "1"}])
        result = d1_api.get_request_status(rid)[1]
        assert result["status"] == "completed"
        assert len(result["result_summary"]["menu_summary"]["recipe_ids"]) == 5

    def test_replace_falls_back_legacy(self) -> None:
        """replace 意图进入 legacy；模型不可用时进入 failed 终态。"""
        rid = _fresh_rid()
        _reset_d1(rid)
        runner = _make_orchestrator(
            has_current_menu=True, llm=_UnavailableLLM())
        runner.run(rid, "sess_multi", "换成清淡的汤", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(rid)[1]["status"]
        assert status == "failed"

    def test_add_constraint_delta(self) -> None:
        """约束追加（有前文菜单）→ 确定性 delta 链路走通（最小修改，不 fallback）。"""
        rid = _fresh_rid()
        _reset_d1(rid)
        runner = _make_orchestrator(has_current_menu=True)
        runner.run(rid, "sess_add", "别做辣的", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(rid)[1]["status"]
        # FakeC4 不真实存储临时约束，当前菜单（1/2/3）仍安全 → 菜单不变但 completed
        assert status == "completed", f"约束追加 delta 未 completed: {status}"

    def test_second_turn_passes_committed_query_plan_to_qwen_and_rag(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """生产编排入口把上一轮餐次/排除条件交给本轮 QueryNormalizer。"""
        previous = {
            "rewritten_query": "晚餐 豆腐",
            "meal_types": ["晚餐"],
            "exclude_ingredients": ["花生"],
            "dish_count_requested": 3,
        }
        captured = {}
        from food_agent_v2.c3 import tool_handler

        real_projection = tool_handler._retrieval_filters_from_query_plan

        def _capture_projection(query_plan):
            captured["query_plan"] = query_plan
            return real_projection(query_plan)

        monkeypatch.setattr(
            tool_handler, "_retrieval_filters_from_query_plan", _capture_projection)

        class _ContextAwareLLM:
            payload = None

            def invoke(self, _role, _system_prompt, user_message, **_kwargs):
                self.payload = json.loads(user_message)
                return {"content": json.dumps({
                    "retrieval_query": "清淡",
                    "taste_tags": ["清淡"],
                }, ensure_ascii=False)}

        llm = _ContextAwareLLM()
        rid = _fresh_rid()
        _reset_d1(rid)
        runner = _make_orchestrator(
            has_current_menu=True, llm=llm, query_plan=previous)

        runner.run(rid, "sess_previous", "再清淡一点",
                   [{"participant_ref": "p1", "user_id": "1"}])

        assert llm.payload["previous_query_plan"] == previous
        query_plan = captured["query_plan"]
        assert query_plan.meal_types == ("晚餐",)
        assert query_plan.exclude_ingredients == ("花生",)
        assert query_plan.dish_count_requested == 3

    def test_new_recommendation_does_not_inherit_previous_query_plan(self) -> None:
        """已有菜单时的新推荐仍从本轮需求开始，不盲目继承旧约束。"""
        class _CaptureLLM:
            payload = None

            def invoke(self, _role, _system_prompt, user_message, **_kwargs):
                self.payload = json.loads(user_message)
                return {"content": json.dumps({
                    "retrieval_query": "午餐 家常",
                    "meal_types": ["午餐"],
                    "taste_tags": ["家常"],
                }, ensure_ascii=False)}

        llm = _CaptureLLM()
        rid = _fresh_rid()
        _reset_d1(rid)
        runner = _make_orchestrator(
            has_current_menu=True,
            llm=llm,
            query_plan={"meal_types": ["晚餐"], "exclude_ingredients": ["花生"]},
        )

        runner.run(rid, "sess_new", "午餐家常菜",
                   [{"participant_ref": "p1", "user_id": "1"}])

        assert llm.payload["previous_query_plan"] is None
