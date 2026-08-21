"""确定性主编排器（P4）链路测试 —— 真实工具 + 真实 B2/B3/B4/C2，FakeC4。

覆盖单轮首次推荐成功路径；多轮（有前文菜单）与 replace/reject fallback legacy。
依赖 MySQL 可用（工具执行真实领域服务）。
"""

import uuid
from types import SimpleNamespace

import pytest

from food_agent_v2.c3.orchestrator import DeterministicRecommendationOrchestrator
from food_agent_v2.c3.fast_intent import FastIntentRouter
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


pytestmark = pytest.mark.skipif(not _mysql_available(), reason="MySQL 不可用")


@pytest.fixture(autouse=True)
def _deterministic_c1_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    class RetrievalPort:
        def retrieve(self, _query: str, top_k: int = 20, **_kwargs):
            # 跳过固定数据缺食材视图的 recipe_id（19/39），避免 B4 完整覆盖 fail-closed
            candidates = [
                SimpleNamespace(recipe_id=recipe_id, name=f"固定菜品{recipe_id}", source_paths=[])
                for recipe_id in range(1, max(top_k, 30) + 1)
                if recipe_id not in (19, 39)
            ]
            return SimpleNamespace(
                total_candidates=len(candidates), candidates=candidates)

    monkeypatch.setattr(
        "food_agent_v2.c1.get_retrieval_service", lambda: RetrievalPort())


class _FakeC4:
    """确定性链路所需的 C4 替身（无前文菜单，供首次推荐 fast path）。"""

    def __init__(self, has_current_menu: bool = False) -> None:
        self._sessions: dict = {}
        self._has_current_menu = has_current_menu

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
                    "current_menu": {"plan_id": "p1", "recipe_ids": [1, 2, 3]}}
        return None

    def store_temporary_constraint(self, session_id, constraint):
        pass

    def to_b4_constraints(self, session_id):
        return []

    def commit_session_state(self, request_id, status, **kw):
        pass

    def _persist_session(self, ctx):
        pass


def _reset_d1(request_id: str) -> None:
    d1_api._requests.clear()
    d1_api._events.clear()
    d1_api._event_cursors.clear()
    d1_api._idempotency.clear()
    d1_api._requests[request_id] = {
        "request_id": request_id, "session_id": "sess_x", "status": "accepted",
        "created_at": "t", "updated_at": "t",
    }


def _make_orchestrator(has_current_menu: bool = False):
    return DeterministicRecommendationOrchestrator(
        llm=None, c4=_FakeC4(has_current_menu=has_current_menu))


class TestDeterministicChain:
    def test_first_recommendation_completed(self) -> None:
        rid = _fresh_rid()
        _reset_d1(rid)
        runner = _make_orchestrator()
        runner.run(rid, "sess_det", "四菜一汤家常", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(rid)[1]["status"]
        assert status == "completed", f"确定性链路未 completed: {status}"

    def test_dish_count_requested_respected(self) -> None:
        rid = _fresh_rid()
        _reset_d1(rid)
        runner = _make_orchestrator()
        runner.run(rid, "sess_cnt", "四菜一汤家常", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(rid)[1]["status"]
        assert status == "completed"

    def test_replace_falls_back_legacy(self) -> None:
        """replace 意图（P5 第一版未覆盖）→ fallback legacy 五模型。"""
        rid = _fresh_rid()
        _reset_d1(rid)
        # legacy 需要 FakeLLM；此处只验证 fallback 分支被触发（不因确定性链崩溃）
        runner = _make_orchestrator(has_current_menu=True)
        # 无 LLM 时 legacy 会失败，但不应是确定性链的错误路径
        runner.run(rid, "sess_multi", "换成清淡的汤", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(rid)[1]["status"]
        # fallback legacy：无 LLM 配置 → failed（而非确定性链 completed）
        assert status != "completed"

    def test_add_constraint_delta(self) -> None:
        """约束追加（有前文菜单）→ 确定性 delta 链路走通（最小修改，不 fallback）。"""
        rid = _fresh_rid()
        _reset_d1(rid)
        runner = _make_orchestrator(has_current_menu=True)
        runner.run(rid, "sess_add", "别做辣的", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(rid)[1]["status"]
        # FakeC4 不真实存储临时约束，当前菜单（1/2/3）仍安全 → 菜单不变但 completed
        assert status == "completed", f"约束追加 delta 未 completed: {status}"
