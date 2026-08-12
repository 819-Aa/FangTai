"""MC-01/MC-01-R1：安全候选权威来源 + 业务终态不依赖模型后续响应。

- handler 级：唯一权威 HealthEvaluationReceipt；retrieval 不得恢复候选；
  模型非权威 safe → SAFE_RECIPE_IDS_MISMATCH；无回执 → HEALTH_EVALUATION_REQUIRED。
- runner 级：safe=[] 精确 no_safe_menu；safe 非空无方案 → no_feasible_menu；
  generate 返回业务终态后模型不再被调用（即使预设模型后续会空输出/抛异常）。
- 持久化边界：no_safe_menu 无 commit/outbox/menu 写调用（用 fake 证明，不连真实库）。
- 测试隔离：不连真实 MySQL，不通过全局 d1_api 写真实 Redis。
"""

from types import SimpleNamespace
from unittest.mock import patch

from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.state import RequestStatus, WorkflowState
from food_agent_v2.c3.tool_handler import ToolContext, _generate_feasible_menus

RID = "11111111-1111-1111-1111-111111111111"
BID = "22222222-2222-2222-2222-222222222222"


def _receipt(safe: list[int]):
    """最小 HealthEvaluationReceipt 替身（真实类型）。"""
    from food_agent_v2.b4.schemas import HealthEvaluationReceipt
    return HealthEvaluationReceipt(
        evaluation_id="e1", request_id=RID, retrieval_result_ref=None,
        constraint_set_refs=[], participant_recipe_results=[],
        safe_recipe_ids=safe, excluded_recipe_ids=[], input_fingerprint="0" * 64)


def _ctx() -> ToolContext:
    return ToolContext(request_id=RID, node_id="health_menu_planning", build_id=BID)


class TestGenerateFeasibleMenus:
    """handler 级：权威回执唯一候选来源（无 DB 依赖）。"""

    def test_safe_empty_ignores_retrieval(self) -> None:
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([])
        ctx.previous_results["retrieval"] = SimpleNamespace(candidates=[
            SimpleNamespace(recipe_id=101)])
        with patch("food_agent_v2.c2.MenuPlanner") as mp:
            result = _generate_feasible_menus({"safe_recipe_ids": [], "dish_count": 4}, ctx)
            mp.assert_not_called()  # 未调用 MenuPlanner
        assert result["plans"] == []
        assert result["note"] == "no_safe_menu"
        assert "feasible_menus" not in ctx.previous_results

    def test_model_safe_outside_authoritative_mismatch(self) -> None:
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([1, 2])
        result = _generate_feasible_menus({"safe_recipe_ids": [1, 99], "dish_count": 4}, ctx)
        assert result.get("error") == "SAFE_RECIPE_IDS_MISMATCH"
        assert result["plans"] == []

    def test_no_health_receipt_required(self) -> None:
        ctx = _ctx()
        ctx.previous_results["retrieval"] = SimpleNamespace(candidates=[
            SimpleNamespace(recipe_id=101)])
        result = _generate_feasible_menus({"safe_recipe_ids": [], "dish_count": 4}, ctx)
        assert result.get("error") == "HEALTH_EVALUATION_REQUIRED"
        assert result["plans"] == []

    def test_non_receipt_object_rejected(self) -> None:
        """任意带 safe_recipe_ids 字段的非回执对象 → HEALTH_EVALUATION_REQUIRED。"""
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = SimpleNamespace(safe_recipe_ids=[1, 2])
        result = _generate_feasible_menus({"safe_recipe_ids": [], "dish_count": 4}, ctx)
        assert result.get("error") == "HEALTH_EVALUATION_REQUIRED"


class TestBuildDualArtifacts:
    """runner 级：_build_dual_artifacts 按权威回执区分终态（无 DB）。"""

    def _state(self) -> WorkflowState:
        return WorkflowState(request_id=RID, build_id=BID,
                             status=RequestStatus.RUNNING,
                             current_node="health_menu_planning",
                             participant_refs=["p1"])

    def _runner(self) -> WorkflowRunner:
        return WorkflowRunner(
            build_id=BID, llm=SimpleNamespace(invoke=lambda *a, **k: {}),
            c4=SimpleNamespace(commit_session_state=lambda *a, **k: None))

    def test_safe_empty_returns_no_safe_menu_terminal(self) -> None:
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([])
        new_state, artifact = self._runner()._build_dual_artifacts(self._state(), ctx)
        assert new_state.is_terminal()
        assert new_state.status == RequestStatus.NO_SAFE_MENU
        assert artifact is None

    def test_safe_nonempty_no_plans_returns_no_feasible_menu(self) -> None:
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([1, 2])
        ctx.previous_results["feasible_menus"] = []
        new_state, artifact = self._runner()._build_dual_artifacts(self._state(), ctx)
        assert new_state.is_terminal()
        assert new_state.status == RequestStatus.NO_FEASIBLE_MENU
        assert artifact is None


class TestControlChain:
    """真实控制链：_call_model 在业务终态后立即停止，模型不再被调用。"""

    def _model_ctx(self) -> SimpleNamespace:
        return SimpleNamespace(role="health_menu_planning", conversation_visible=[],
                               constraint_visible=[], menu_visible={})

    def test_no_safe_menu_stops_model_chain(self) -> None:
        """generate 返回 no_safe_menu → _call_model 立即返回 terminal，模型只被调 2 次。"""
        from food_agent_v2.c3 import ROLE_POLICIES

        calls: list[str] = []

        class _FakeLLM:
            def invoke(self, role, *a, **k):
                calls.append(role)
                assert role == "health_menu_planning", f"不应进入 {role}"
                if len(calls) == 1:
                    return {"content": "", "tool_calls": [
                        {"name": "get_health_constraints", "arguments": {}},
                        {"name": "evaluate_recipe_health", "arguments": {"recipe_ids": [1, 2]}}]}
                if len(calls) == 2:
                    return {"content": "", "tool_calls": [
                        {"name": "generate_feasible_menus",
                         "arguments": {"safe_recipe_ids": [], "dish_count": 4}}]}
                raise AssertionError(f"模型不应被再次调用（第 {len(calls)} 次）")

        def fake_evaluate(args, ctx):
            ctx.previous_results["health_evaluation"] = _receipt([])
            ctx.safe_recipe_ids = []
            return {"safe_recipe_ids": [], "excluded_recipe_ids": [], "safe_count": 0}

        import food_agent_v2.c3.tool_handler as th
        with patch.dict(th._TOOL_MAP, {
                "evaluate_recipe_health": fake_evaluate,
                "get_health_constraints": lambda args, ctx: {"participants": {}}}):
            runner = WorkflowRunner(build_id=BID, llm=_FakeLLM())
            ctx = _ctx()
            result = runner._call_model("health_menu_planning",
                                        ROLE_POLICIES["health_menu_planning"],
                                        self._model_ctx(), "健康审查并生成菜单方案",
                                        ctx)
        assert result.get("status") == "terminal"
        assert result.get("terminal") == "no_safe_menu"
        assert calls == ["health_menu_planning", "health_menu_planning"]
        assert "feasible_menus" not in ctx.previous_results

    def test_no_feasible_menu_stops_model_chain(self) -> None:
        """safe 非空但 planner 无方案 → 精确 no_feasible_menu，模型不再被调用。"""
        from food_agent_v2.c3 import ROLE_POLICIES

        calls: list[str] = []

        class _FakeLLM:
            def invoke(self, role, *a, **k):
                calls.append(role)
                assert role == "health_menu_planning", f"不应进入 {role}"
                if len(calls) == 1:
                    return {"content": "", "tool_calls": [
                        {"name": "get_health_constraints", "arguments": {}},
                        {"name": "evaluate_recipe_health", "arguments": {"recipe_ids": [1, 2]}}]}
                if len(calls) == 2:
                    return {"content": "", "tool_calls": [
                        {"name": "generate_feasible_menus",
                         "arguments": {"safe_recipe_ids": [1, 2], "dish_count": 4}}]}
                raise AssertionError(f"模型不应被再次调用（第 {len(calls)} 次）")

        def fake_evaluate(args, ctx):
            ctx.previous_results["health_evaluation"] = _receipt([1, 2])
            ctx.safe_recipe_ids = [1, 2]
            return {"safe_recipe_ids": [1, 2], "excluded_recipe_ids": [], "safe_count": 2}

        import food_agent_v2.c2 as c2mod
        import food_agent_v2.c3.tool_handler as th
        # 真实 _generate_feasible_menus：MenuPlanner.plan 返回 [] → no_feasible_menu
        with patch.dict(th._TOOL_MAP, {
                "evaluate_recipe_health": fake_evaluate,
                "get_health_constraints": lambda args, ctx: {"participants": {}}}), \
                patch.object(c2mod.MenuPlanner, "plan", return_value=[]), \
                patch.object(c2mod.MenuPlanner, "set_safe_candidates"), \
                patch.object(c2mod.MenuPlanner, "set_recipe_features"):
            runner = WorkflowRunner(build_id=BID, llm=_FakeLLM())
            ctx = _ctx()
            result = runner._call_model("health_menu_planning",
                                        ROLE_POLICIES["health_menu_planning"],
                                        self._model_ctx(), "健康审查并生成菜单方案",
                                        ctx)
        assert result.get("status") == "terminal"
        assert result.get("terminal") == "no_feasible_menu"
        assert calls == ["health_menu_planning", "health_menu_planning"]


class TestNoSafeTerminalPersistence:
    """no_safe_menu 终态：无 commit/outbox/menu 写调用（fake 证明，不连真实库/Redis）。"""

    def test_no_safe_menu_finalize_no_persistence(self) -> None:
        from food_agent_v2.c3.runner import WorkflowRunner
        from food_agent_v2.d1 import api as d1_api

        class _FakeLLM:
            def invoke(self, *a, **k):
                raise AssertionError("不应调用模型")

        class _C4:
            def commit_session_state(self, request_id, status, **kw):
                pass

        rid = f"t-nosafe-{RID[-8:]}"
        # 用真实 D1 但禁用 Redis 持久化（不写真实 Redis）
        with patch.object(d1_api, "_persist_request", lambda request_id: None):
            d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                     "session_id": "s_ns"}
            state = WorkflowState(request_id=rid, build_id=BID,
                                  status=RequestStatus.NO_SAFE_MENU)
            runner = WorkflowRunner(build_id=BID, llm=_FakeLLM(), c4=_C4())
            # 证明不调用 commit_request_result（⇒ 无 menu_versions/成功 outbox 写路径）
            with patch("food_agent_v2.application.commit_request_result") as commit:
                runner._finalize(state, rid, _C4(), lock_token="1")
                commit.assert_not_called()
            types = [e["event"] for e in d1_api.subscribe_events(rid)]
            assert "request_terminal" in types
            term = next(e for e in d1_api.subscribe_events(rid)
                        if e["event"] == "request_terminal")
            import json as _json
            assert _json.loads(term["data"])["status"] == "no_safe_menu"
            assert "answer_ready" not in types
            assert "result_committed" not in types
            assert "error" not in types
