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

import pytest

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

    def test_query_plan_dish_count_overrides_model_tool_argument(self) -> None:
        """工具参数不得把 QueryPlan 已确认的 5 道篡改成 3 道。"""
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([1, 2, 3, 4, 5, 6])
        ctx.previous_results["query_plan"] = SimpleNamespace(
            dish_count_requested=5,
            dish_types=("汤",),
            flavor_preferences=("家常",),
            nutrition_goal_codes=("low_sodium",),
        )
        with patch("food_agent_v2.c2.MenuPlanner") as planner_cls:
            planner_cls.return_value.plan.return_value = []
            _generate_feasible_menus(
                {"safe_recipe_ids": [1, 2, 3, 4, 5, 6], "dish_count": 3}, ctx)
        hard = planner_cls.return_value.plan.call_args.args[0]
        assert hard.dish_count == 5
        assert hard.require_soup is True
        assert planner_cls.return_value.plan.call_args.kwargs["nutrition_goal_codes"] == (
            "low_sodium",
        )

    def test_c1_rank_becomes_c2_preference_evidence(self) -> None:
        """健康过滤后仍保留 C1 相关性顺序，不能按 recipe_id 重新选菜。"""
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([10, 20, 30])
        ctx.previous_results["retrieval"] = SimpleNamespace(candidates=[
            SimpleNamespace(recipe_id=30, score=0.9, rerank_score=0.95),
            SimpleNamespace(recipe_id=10, score=0.7, rerank_score=0.8),
            SimpleNamespace(recipe_id=20, score=0.6, rerank_score=0.7),
        ])
        with (
            patch("food_agent_v2.c2.MenuPlanner") as planner_cls,
            patch("food_agent_v2.b3.recipe_views.get_view_builder") as builder,
        ):
            builder.return_value.build_retrieval_view.side_effect = lambda rid: (
                SimpleNamespace(name=f"菜{rid}", searchable_fields={})
            )
            planner_cls.return_value.plan.return_value = []
            _generate_feasible_menus(
                {"safe_recipe_ids": [10, 20, 30], "dish_count": 3}, ctx)

        features = planner_cls.return_value.set_recipe_features.call_args.args[0]
        assert features[30]["preference_score"] > features[10]["preference_score"]
        assert features[10]["preference_score"] > features[20]["preference_score"]


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


class TestRunModelNodeTerminalValidation:
    """MC-01-R2 P0-1：真实 _run_model_node 校验通过后才进入业务终态。"""

    def _run_health(self, fake_llm, evaluate_fn):
        import food_agent_v2.c3.tool_handler as th
        from food_agent_v2.c3 import NodeValidator

        class _C4:
            def project_model_context(self, role, handoff, ref):
                return SimpleNamespace(role=role, conversation_visible=[],
                                       constraint_visible=[], menu_visible={})
            def commit_session_state(self, request_id, status, **kw):
                pass

        state = WorkflowState(request_id=RID, build_id=BID, status=RequestStatus.RUNNING,
                              current_node="health_menu_planning", participant_refs=["p1"])
        ctx = _ctx()
        runner = WorkflowRunner(build_id=BID, llm=fake_llm, c4=_C4())
        with patch.dict(th._TOOL_MAP, {
                "evaluate_recipe_health": evaluate_fn,
                "get_health_constraints": lambda a, c: {"participants": {}},
                "expand_retrieval": lambda a, c: (_ for _ in ()).throw(
                    AssertionError("expand_retrieval 不应执行"))}), \
                patch.object(NodeValidator, "pre_check", return_value=None):
            new_state, _r, _a = runner._run_model_node(
                state, _C4(), ctx, "health_menu_planning", "msg")
        return new_state, ctx

    def test_real_run_model_node_no_safe_menu(self) -> None:
        """真实 _run_model_node 进入 no_safe_menu（非直调 _call_model）。"""
        calls: list[str] = []

        class _FakeLLM:
            def invoke(self, role, *a, **k):
                calls.append(role)
                if len(calls) == 1:
                    return {"content": "", "tool_calls": [
                        {"name": "get_health_constraints", "arguments": {}},
                        {"name": "evaluate_recipe_health", "arguments": {"recipe_ids": [1, 2]}}]}
                if len(calls) == 2:
                    return {"content": "", "tool_calls": [
                        {"name": "generate_feasible_menus",
                         "arguments": {"safe_recipe_ids": [], "dish_count": 4}}]}
                raise AssertionError(f"模型不应被再次调用（第 {len(calls)} 次）")

        def fake_eval(a, c):
            c.previous_results["health_evaluation"] = _receipt([])
            c.safe_recipe_ids = []
            return {"safe_recipe_ids": [], "safe_count": 0}

        new_state, ctx = self._run_health(_FakeLLM(), fake_eval)
        assert new_state.status == RequestStatus.NO_SAFE_MENU
        assert calls == ["health_menu_planning", "health_menu_planning"]
        # 终态节点回执已记录进 WorkflowState
        assert len(new_state.tool_receipts) >= 3  # constraints + evaluate + generate

    def test_required_tools_missing_cannot_enter_no_safe(self) -> None:
        """必需工具缺失 → post_check 拒绝，不得进入 no_safe_menu。"""
        import food_agent_v2.c3.tool_handler as th
        from food_agent_v2.c3 import NodeValidator

        class _C4:
            def project_model_context(self, role, handoff, ref):
                return SimpleNamespace(role=role, conversation_visible=[],
                                       constraint_visible=[], menu_visible={})
            def commit_session_state(self, request_id, status, **kw):
                pass
        calls: list[str] = []

        class _NoEvalLLM:
            def invoke(self, role, *a, **k):
                calls.append(role)
                # 只调 constraints + generate（缺 evaluate）；第 2 轮正常结束
                if len(calls) == 1:
                    return {"content": "", "tool_calls": [
                        {"name": "get_health_constraints", "arguments": {}},
                        {"name": "generate_feasible_menus",
                         "arguments": {"safe_recipe_ids": [], "dish_count": 4}}]}
                return {"content": "{}", "tool_calls": []}
        state = WorkflowState(request_id=RID, build_id=BID, status=RequestStatus.RUNNING,
                              current_node="health_menu_planning", participant_refs=["p1"])
        ctx = _ctx()
        runner = WorkflowRunner(build_id=BID, llm=_NoEvalLLM(), c4=_C4())
        with patch.dict(th._TOOL_MAP, {
                "get_health_constraints": lambda a, c: {"participants": {}}}), \
                patch.object(NodeValidator, "pre_check", return_value=None):
            new_state, _r, _a = runner._run_model_node(
                state, _C4(), ctx, "health_menu_planning", "msg")
        assert new_state.is_terminal()
        assert new_state.status != RequestStatus.NO_SAFE_MENU
        assert new_state.error.error_code == "REQUIRED_TOOL_NOT_CALLED"

    def test_failed_required_receipt_cannot_enter_terminal(self) -> None:
        """失败必需回执 → TOOL_EXECUTION_FAILED，不得进入业务终态。"""
        import food_agent_v2.c3.tool_handler as th
        from food_agent_v2.c3 import NodeValidator

        class _C4:
            def project_model_context(self, role, handoff, ref):
                return SimpleNamespace(role=role, conversation_visible=[],
                                       constraint_visible=[], menu_visible={})
            def commit_session_state(self, request_id, status, **kw):
                pass
        calls: list[str] = []

        class _FailedEvalLLM:
            def invoke(self, role, *a, **k):
                calls.append(role)
                if len(calls) == 1:
                    return {"content": "", "tool_calls": [
                        {"name": "get_health_constraints", "arguments": {}},
                        {"name": "evaluate_recipe_health", "arguments": {"recipe_ids": [1, 2]}}]}
                if len(calls) == 2:
                    return {"content": "", "tool_calls": [
                        {"name": "generate_feasible_menus",
                         "arguments": {"safe_recipe_ids": [], "dish_count": 4}}]}
                return {"content": "{}", "tool_calls": []}

        def failed_eval(a, c):
            return {"error": "TOOL_EXECUTION_FAILED: boom", "safe_recipe_ids": []}
        state = WorkflowState(request_id=RID, build_id=BID, status=RequestStatus.RUNNING,
                              current_node="health_menu_planning", participant_refs=["p1"])
        ctx = _ctx()
        runner = WorkflowRunner(build_id=BID, llm=_FailedEvalLLM(), c4=_C4())
        with patch.dict(th._TOOL_MAP, {
                "evaluate_recipe_health": failed_eval,
                "get_health_constraints": lambda a, c: {"participants": {}}}), \
                patch.object(NodeValidator, "pre_check", return_value=None):
            new_state, _r, _a = runner._run_model_node(
                state, _C4(), ctx, "health_menu_planning", "msg")
        assert new_state.is_terminal()
        assert new_state.status != RequestStatus.NO_SAFE_MENU
        assert new_state.error.error_code == "TOOL_EXECUTION_FAILED"

    def test_trailing_tool_after_terminal_never_executes(self) -> None:
        """generate 返回终态后，同批 trailing 工具（expand_retrieval）绝不执行。"""
        calls: list[str] = []

        class _FakeLLM:
            def invoke(self, role, *a, **k):
                calls.append(role)
                if len(calls) == 1:
                    return {"content": "", "tool_calls": [
                        {"name": "get_health_constraints", "arguments": {}},
                        {"name": "evaluate_recipe_health", "arguments": {"recipe_ids": [1, 2]}}]}
                if len(calls) == 2:
                    return {"content": "", "tool_calls": [
                        {"name": "generate_feasible_menus",
                         "arguments": {"safe_recipe_ids": [], "dish_count": 4}},
                        {"name": "expand_retrieval", "arguments": {}}]}
                raise AssertionError(f"模型不应被再次调用（第 {len(calls)} 次）")

        def fake_eval(a, c):
            c.previous_results["health_evaluation"] = _receipt([])
            c.safe_recipe_ids = []
            return {"safe_recipe_ids": [], "safe_count": 0}

        # expand_retrieval 在 _run_health 中被替换为必抛函数；若被调用则测试失败
        new_state, _ctx_ = self._run_health(_FakeLLM(), fake_eval)
        assert new_state.status == RequestStatus.NO_SAFE_MENU
        assert calls == ["health_menu_planning", "health_menu_planning"]


class TestErrorCodeWhitelist:
    """MC-01-R2 P1：错误码白名单化。"""

    def _post_check_receipt(self, error_code):
        from uuid import UUID as _UUID

        from food_agent_v2.c3 import ROLE_POLICIES, NodeValidator
        from food_agent_v2.c3.receipts import ToolReceipt
        from food_agent_v2.c3.state import WorkflowState
        policy = ROLE_POLICIES["health_menu_planning"]
        state = WorkflowState(request_id=RID, build_id=BID,
                              current_node="health_menu_planning")

        def _rec(tool_name, success, err=None):
            return ToolReceipt(
                request_id=_UUID(RID), node_id="health_menu_planning",
                tool_call_id=f"c-{tool_name}", tool_name=tool_name,
                input_hash="a" * 64, output_hash="b" * 64,
                build_id=_UUID(BID), success=success, error_code=err)
        # 所有必需工具：constraints/evaluate 成功；generate 按 error_code 失败
        receipts = [
            _rec("get_health_constraints", True),
            _rec("evaluate_recipe_health", True),
            _rec("generate_feasible_menus", False, error_code),
        ]
        return NodeValidator.post_check(state, policy, None, receipts)

    def test_unknown_error_code_normalized(self) -> None:
        """动态/未知 error_code → 规范化为 TOOL_EXECUTION_FAILED。"""
        err = self._post_check_receipt("SOME:dynamic:garbage:abc")
        assert err.error_code == "TOOL_EXECUTION_FAILED"
        assert "garbage" not in err.message and "SOME" not in err.message

    def test_allowed_codes_stay_precise(self) -> None:
        """两个白名单错误码保持精确。"""
        err1 = self._post_check_receipt("SAFE_RECIPE_IDS_MISMATCH")
        assert err1.error_code == "SAFE_RECIPE_IDS_MISMATCH"
        err2 = self._post_check_receipt("HEALTH_EVALUATION_REQUIRED")
        assert err2.error_code == "HEALTH_EVALUATION_REQUIRED"


@pytest.fixture(autouse=True)
def _clean_global_d1():
    """MC-01-R2 测试 10：结束后清理全局 D1 requests/events/cursors（固定 ID 不污染）。"""
    from food_agent_v2.d1 import api as d1_api
    yield
    d1_api._requests.clear()
    d1_api._events.clear()
    d1_api._event_cursors.clear()
    d1_api._idempotency.clear()


class TestStrictTimeIndeterminate:
    """严格时限 + B5 无法高权威判定 → strict_time_indeterminate（INV-021）。"""

    def test_strict_time_unknown_returns_indeterminate(self) -> None:
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([1, 2, 3])
        ctx.time_limit_minutes = 10  # 查询理解提取的严格时间约束

        from food_agent_v2.c2 import MenuPlanner
        from food_agent_v2.c3.tool_handler import _generate_feasible_menus

        class _FakeB5:
            def compute_menu_schedule(self, recipe_ids, time_limit_minutes):
                from types import SimpleNamespace
                return SimpleNamespace(
                    strict_time_feasible="unknown",
                    authority="deterministic_partial",
                    missing_facts=("recipe:1/step:2",),
                )

        with patch.object(MenuPlanner, "plan", return_value=[]), \
             patch("food_agent_v2.b5.get_time_service",
                   return_value=_FakeB5()):
            result = _generate_feasible_menus(
                {"safe_recipe_ids": [1, 2, 3], "dish_count": 3}, ctx)
        assert result["note"] == "strict_time_indeterminate"
        assert result["plans"] == []
        assert result["safe_count"] == 3

    def test_strict_time_false_returns_no_feasible(self) -> None:
        """B5 明确 false（不是 unknown）→ 仍按 no_feasible_menu（时间确定不可行）。"""
        ctx = _ctx()
        ctx.previous_results["health_evaluation"] = _receipt([1, 2, 3])
        ctx.time_limit_minutes = 10

        from food_agent_v2.c2 import MenuPlanner
        from food_agent_v2.c3.tool_handler import _generate_feasible_menus

        class _FakeB5:
            def compute_menu_schedule(self, recipe_ids, time_limit_minutes):
                from types import SimpleNamespace
                return SimpleNamespace(
                    strict_time_feasible=False,
                    authority="deterministic_high",
                    missing_facts=(),
                )

        with patch.object(MenuPlanner, "plan", return_value=[]), \
             patch("food_agent_v2.b5.get_time_service",
                   return_value=_FakeB5()):
            result = _generate_feasible_menus(
                {"safe_recipe_ids": [1, 2, 3], "dish_count": 3}, ctx)
        assert result["note"] == "no_feasible_menu"
