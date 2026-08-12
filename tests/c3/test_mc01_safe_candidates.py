"""MC-01：安全候选越权回退修复 —— 权威健康回执是唯一候选来源。

- generate_feasible_menus 不得从 retrieval 恢复候选；
- 模型传入非权威 safe_recipe_ids → SAFE_RECIPE_IDS_MISMATCH；
- 无健康回执 → HEALTH_EVALUATION_REQUIRED；
- safe=[] → no_safe_menu（不调用 MenuPlanner、不进入后续节点）；
- safe 非空但无方案 → no_feasible_menu；
- no_safe_menu 后无 answer_ready/result_committed、无 menu_versions、无成功 outbox。
"""

from types import SimpleNamespace

from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.state import RequestStatus, WorkflowState
from food_agent_v2.c3.tool_handler import ToolContext, _generate_feasible_menus
from food_agent_v2.d1 import api as d1_api

RID = "11111111-1111-1111-1111-111111111111"
BID = "22222222-2222-2222-2222-222222222222"


def _receipt(safe: list[int]) -> SimpleNamespace:
    """最小 HealthEvaluationReceipt 替身。"""
    return SimpleNamespace(
        safe_recipe_ids=safe,
        excluded_recipe_ids=[],
        participant_recipe_results=[],
    )


def _ctx() -> ToolContext:
    return ToolContext(request_id=RID, node_id="health_menu_planning", build_id=BID)


class TestGenerateFeasibleMenus:
    """handler 级：权威回执唯一候选来源。"""

    def test_safe_empty_ignores_retrieval(self) -> None:
        """B4 safe=[] 即使 retrieval 有候选 → plans=[]（不恢复候选）。"""
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([])
        ctx.previous_results["retrieval"] = SimpleNamespace(candidates=[
            SimpleNamespace(recipe_id=101),
            SimpleNamespace(recipe_id=102),
        ])
        result = _generate_feasible_menus({"safe_recipe_ids": [], "dish_count": 4}, ctx)
        assert result["plans"] == []
        assert result["note"] == "no_safe_menu"
        # 未调用 MenuPlanner：feasible_menus 未被写入
        assert "feasible_menus" not in ctx.previous_results

    def test_model_safe_outside_authoritative_mismatch(self) -> None:
        """模型传入权威集合之外的 recipe_id → SAFE_RECIPE_IDS_MISMATCH。"""
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([1, 2])
        result = _generate_feasible_menus({"safe_recipe_ids": [1, 99], "dish_count": 4}, ctx)
        assert result.get("error") == "SAFE_RECIPE_IDS_MISMATCH"
        assert result["plans"] == []

    def test_no_health_receipt_required(self) -> None:
        """无权威健康回执 → HEALTH_EVALUATION_REQUIRED。"""
        ctx = _ctx()
        ctx.previous_results["retrieval"] = SimpleNamespace(candidates=[
            SimpleNamespace(recipe_id=101)])
        result = _generate_feasible_menus({"safe_recipe_ids": [], "dish_count": 4}, ctx)
        assert result.get("error") == "HEALTH_EVALUATION_REQUIRED"
        assert result["plans"] == []

    def test_authoritative_safe_used_and_model_ignored(self) -> None:
        """模型列表被权威回执覆盖：不在权威集合的模型 id 被拒绝。"""
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([1, 2, 3])
        result = _generate_feasible_menus({"safe_recipe_ids": [2], "dish_count": 4}, ctx)
        # 模型传子集（均权威内）→ 不报错，且仍按权威候选尝试规划（可空方案）
        assert "error" not in result
        assert "safe_count" in result


class TestBuildDualArtifacts:
    """runner 级：_build_dual_artifacts 按权威回执区分终态。"""

    def _state(self) -> WorkflowState:
        return WorkflowState(request_id=RID, build_id=BID,
                             status=RequestStatus.RUNNING,
                             current_node="health_menu_planning",
                             participant_refs=["p1"])

    def test_safe_empty_returns_no_safe_menu_terminal(self) -> None:
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([])
        state = self._state()
        runner = WorkflowRunner(build_id=BID, llm=SimpleNamespace(invoke=lambda *a, **k: {}),
                                c4=SimpleNamespace(commit_session_state=lambda *a, **k: None))
        new_state, artifact = runner._build_dual_artifacts(state, ctx)
        assert new_state.is_terminal()
        assert new_state.status == RequestStatus.NO_SAFE_MENU
        assert artifact is None

    def test_safe_nonempty_no_plans_returns_no_feasible_menu(self) -> None:
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([1, 2])
        ctx.previous_results["feasible_menus"] = []
        state = self._state()
        runner = WorkflowRunner(build_id=BID, llm=SimpleNamespace(invoke=lambda *a, **k: {}),
                                c4=SimpleNamespace(commit_session_state=lambda *a, **k: None))
        new_state, artifact = runner._build_dual_artifacts(state, ctx)
        assert new_state.is_terminal()
        assert new_state.status == RequestStatus.NO_FEASIBLE_MENU
        assert artifact is None


class TestNoSafePersistenceAndSSE:
    """no_safe_menu 终态：SSE 只发 request_terminal，无成功事件；无 menu_versions/成功 outbox。"""

    def test_no_safe_menu_finalize(self) -> None:
        from food_agent_v2.c3.runner import WorkflowRunner
        from food_agent_v2.c3.state import WorkflowState

        class _FakeLLM:
            def invoke(self, *a, **k):
                raise AssertionError("不应调用模型")

        class _C4:
            def commit_session_state(self, request_id, status, **kw):
                pass

        rid = f"t-nosafe-{RID[-8:]}"
        d1_api._requests[rid] = {"request_id": rid, "status": "running", "session_id": "s_ns"}
        state = WorkflowState(request_id=rid, build_id=BID, status=RequestStatus.NO_SAFE_MENU)
        runner = WorkflowRunner(build_id=BID, llm=_FakeLLM(), c4=_C4())
        runner._finalize(state, rid, _C4(), lock_token="1")
        events = d1_api.subscribe_events(rid)
        types = [e["event"] for e in events]
        assert "request_terminal" in types
        term = next(e for e in events if e["event"] == "request_terminal")
        import json as _json
        assert _json.loads(term["data"])["status"] == "no_safe_menu"
        assert "answer_ready" not in types
        assert "result_committed" not in types
        # 持久化边界：no_safe_menu 无 menu_versions、无成功 outbox
        import pymysql

        from food_agent_v2.core.config import load_config
        cfg = load_config().mysql
        conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                               password=cfg.password, database=cfg.database,
                               charset="utf8mb4")
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM menu_versions WHERE session_id=%s", ("s_ns",))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid,))
        assert cur.fetchone()[0] == 0
        conn.close()
