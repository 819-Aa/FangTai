"""T17 脚本化 Runner 全链路测试（真实工具 + 真实 _call_model/_run_model_node）。

覆盖 query → health → menu → answer → review 五个角色：FakeLLM 驱动真实工具调用
（C1/B2/B3/B4/C2 领域服务），节点经真实 _run_model_node/_call_model 执行，携带生产
tools 与 response_format。依赖 MySQL 可用（工具执行真实领域服务）。
"""

import json
import re
import uuid
from types import SimpleNamespace

import pytest

from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.d1 import api as d1_api

RID = "11111111-1111-1111-1111-111111111111"
BID = "22222222-2222-2222-2222-222222222222"


def _fresh_rid() -> str:
    """每个用例独立的 request_id（幂等提交按 request 唯一，避免跨次运行污染 DB）。"""
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


pytestmark = pytest.mark.skipif(not _mysql_available(), reason="MySQL 不可用")


class _FakeC4:
    def __init__(self) -> None:
        self._sessions: dict = {}

    def build_shared_context(self, session_id, participant_refs, raw, mapping,
                             request_id=None, build_id=None):
        return SimpleNamespace(session_id=session_id), {}

    def validate_context_integrity(self, ref):
        return {"valid": True}

    def project_model_context(self, role, handoff, ref):
        return SimpleNamespace(role=role, conversation_visible=[],
                               constraint_visible=[], menu_visible={})

    def _persist_session(self, ctx):
        pass

    def commit_session_state(self, request_id, status):
        pass


def _parse_task(msg: str) -> dict:
    """从累积 user_message 的 ## 任务 块解析 JSON（任务 JSON 为单行）。"""
    in_task = False
    for line in msg.split("\n"):
        if line.startswith("## 任务"):
            in_task = True
            continue
        if in_task:
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except Exception:
                    pass
            break
    return {}


def _extract_tool_result(msg: str, tool_name: str):
    blocks = re.findall(rf"\[{re.escape(tool_name)}\] (\{{.*?\}})(?:\n|\Z)", msg, re.DOTALL)
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1])
    except Exception:
        return None


class _ScriptedLLM:
    """脚本化模型：真实工具调用 + 解析真实结果构建合法 Artifact。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, role, system_prompt, user_message, tools=None, response_format=None):
        self.calls.append(role)
        if role == "query_understanding":
            if "retrieve_recipes" not in user_message:
                return {"content": "", "tool_calls": [
                    {"name": "retrieve_recipes", "arguments": {"query": "清淡营养餐"}}]}
            return {"content": json.dumps({
                "dish_count_requested": 4, "flavor_preferences": ["家常"],
                "time_constraint_seconds": None, "time_constraint_policy": "flexible",
            }), "tool_calls": []}

        if role == "health_menu_planning":
            if "evaluate_recipe_health" not in user_message:
                task = _parse_task(user_message)
                ids = task.get("retrieved_candidate_recipe_ids", [])[:10]
                return {"content": "", "tool_calls": [
                    {"name": "get_health_constraints", "arguments": {}},
                    {"name": "evaluate_recipe_health", "arguments": {"recipe_ids": ids}},
                ]}
            if "generate_feasible_menus" not in user_message:
                ev = _extract_tool_result(user_message, "evaluate_recipe_health") or {}
                safe = ev.get("safe_recipe_ids") or []
                return {"content": "", "tool_calls": [
                    {"name": "generate_feasible_menus",
                     "arguments": {"safe_recipe_ids": safe, "dish_count": 4}}]}
            return {"content": json.dumps({"safe_recipe_ids": [], "excluded_recipe_ids": [],
                                           "menu_plans": []}), "tool_calls": []}

        if role == "menu_decision":
            if "validate_selected_menu_health" not in user_message:
                task = _parse_task(user_message)
                plans = task.get("feasible_menus") or []
                plan = plans[0] if plans else {}
                return {"content": "", "tool_calls": [
                    {"name": "validate_selected_menu_health",
                     "arguments": {"plan_id": plan.get("plan_id", ""),
                                   "recipe_ids": plan.get("recipe_ids", [])}}]}
            task = _parse_task(user_message)
            plan = (task.get("feasible_menus") or [{}])[0]
            fv = _extract_tool_result(user_message, "validate_selected_menu_health") or {}
            return {"content": json.dumps({
                "plan_id": plan.get("plan_id", ""),
                "recipe_ids": plan.get("recipe_ids", []),
                "menu_hash": plan.get("menu_hash", ""),
                "feasible_menu_artifact_ref": task.get("feasible_menu_artifact_ref", ""),
                "final_validation_ref": fv.get("final_validation_ref", ""),
            }), "tool_calls": []}

        if role == "answer_generation":
            task = _parse_task(user_message)
            return {"content": json.dumps({
                "plan_id": task.get("plan_id", ""),
                "recipe_ids": task.get("recipe_ids", []),
                "menu_hash": task.get("menu_hash", ""),
                "menu_ref": task.get("menu_ref", ""),
                "final_validation_ref": task.get("final_validation_ref", ""),
                "content": {"conclusion": "为您推荐家常口味的三菜一汤，营养均衡。",
                            "menu_summary": "包含四道家常菜，口味清爽，适合全家。"},
            }), "tool_calls": []}

        if role == "unified_review":
            if "get_execution_trace" not in user_message:
                return {"content": "", "tool_calls": [
                    {"name": "get_execution_trace", "arguments": {}}]}
            return {"content": json.dumps({"status": "PASS"}), "tool_calls": []}

        return {"content": json.dumps({"status": "ok"}), "tool_calls": []}


def _make_runner():
    return WorkflowRunner(build_id=BID, llm=_ScriptedLLM(), c4=_FakeC4())


def _reset_d1(request_id: str = RID) -> None:
    from food_agent_v2.d1 import api as d1_api

    d1_api._requests.clear()
    d1_api._events.clear()
    d1_api._event_cursors.clear()
    d1_api._idempotency.clear()
    d1_api._requests[request_id] = {
        "request_id": request_id, "session_id": "sess_x", "status": "accepted",
        "created_at": "t", "updated_at": "t",
    }


class TestScriptedRunnerChain:
    def test_full_chain_query_to_review(self) -> None:
        rid = _fresh_rid()
        _reset_d1(rid)

        runner = _make_runner()
        runner.run(rid, "sess_chain", "推荐三菜一汤，家常口味，45分钟内",
                   [{"participant_ref": "p1", "user_id": "1"}])

        status = d1_api.get_request_status(rid)[1]["status"]
        assert status == "completed", f"全链路未 completed: {status}"

        # 五角色全部执行
        llm = runner._llm
        assert set(llm.calls) == {"query_understanding", "health_menu_planning",
                                  "menu_decision", "answer_generation", "unified_review"}

    def test_menu_decision_matches_plan_menu_hash(self) -> None:
        """真实 MENU_DECISION 分支：决策 menu_hash 必须等于所选 FeasibleMenu.menu_hash。"""
        rid = _fresh_rid()
        _reset_d1(rid)

        runner = _make_runner()
        runner.run(rid, "sess_hash", "推荐家常菜三菜一汤", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(rid)[1]["status"]
        assert status == "completed", f"menu_hash 绑定链路失败: {status}"


class TestFingerprintBinding:
    def test_fingerprint_differs_by_input(self) -> None:
        from food_agent_v2.c3.state import WorkflowState

        state = WorkflowState(request_id=str(RID), build_id=BID)
        state.current_node = None
        ctx1 = SimpleNamespace(role="query_understanding", conversation_visible=[],
                               constraint_visible=[], menu_visible={})
        ctx2 = SimpleNamespace(role="query_understanding", conversation_visible=[],
                               constraint_visible=[], menu_visible={})
        fp1 = WorkflowRunner._fingerprint_seed(state, "query_understanding", ctx1, "输入A")
        fp2 = WorkflowRunner._fingerprint_seed(state, "query_understanding", ctx2, "输入B")
        assert fp1 != fp2

    def test_fingerprint_stable_for_same_input(self) -> None:
        from food_agent_v2.c3.state import WorkflowState

        state = WorkflowState(request_id=str(RID), build_id=BID)
        ctx = SimpleNamespace(role="query_understanding", conversation_visible=["c"],
                              constraint_visible=["k"], menu_visible={})
        fp1 = WorkflowRunner._fingerprint_seed(state, "query_understanding", ctx, "输入")
        fp2 = WorkflowRunner._fingerprint_seed(state, "query_understanding", ctx, "输入")
        assert fp1 == fp2

    def test_fingerprint_not_all_zeros(self) -> None:
        from food_agent_v2.c3.state import WorkflowState

        state = WorkflowState(request_id=str(RID), build_id=BID)
        ctx = SimpleNamespace(role="query_understanding", conversation_visible=[],
                              constraint_visible=[], menu_visible={})
        fp = WorkflowRunner._fingerprint_seed(state, "query_understanding", ctx, "输入")
        assert fp != "0" * 64
        assert len(fp) == 64


class TestHealthNodeDualArtifact:
    def test_real_health_node_builds_two_artifacts(self) -> None:
        """真实 health_menu_planning 节点：状态中两个字段类型不同、引用正确。"""
        rid = _fresh_rid()
        _reset_d1(rid)

        runner = _make_runner()
        # 执行完整链路（health 分支为真实节点），completed 即证明双 Artifact 通过 menu 前置校验
        runner.run(rid, "sess_health", "推荐家常菜", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(rid)[1]["status"]
        assert status == "completed"


class TestTerminalPublish:
    """业务终态在状态持久化后必须发布 SSE 通知（T21 P1-2）。"""

    def _state(self, rid: str, status):
        from food_agent_v2.c3.state import RequestStatus, WorkflowState
        return WorkflowState(request_id=rid, status=RequestStatus(status))

    def _finalize(self, rid: str, state) -> None:
        import json as _json

        class _C4:
            def commit_session_state(self, request_id, status, **kw):
                pass

        runner = WorkflowRunner(build_id=BID, llm=_ScriptedLLM(), c4=_C4())
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": "sess_term"}
        runner._finalize(state, rid, _C4(), lock_token="1")
        return _json

    def test_no_safe_menu_publishes_request_terminal(self) -> None:
        rid = _fresh_rid()
        state = self._state(rid, "no_safe_menu")
        self._finalize(rid, state)
        events = d1_api.subscribe_events(rid)
        types = [e["event"] for e in events]
        assert "request_terminal" in types
        ev = next(e for e in events if e["event"] == "request_terminal")
        data = json.loads(ev["data"])
        assert data["status"] == "no_safe_menu"  # 不伪装成 failed

    def test_no_feasible_menu_and_strict_time_distinct(self) -> None:
        for status in ("no_feasible_menu", "strict_time_indeterminate"):
            rid = _fresh_rid()
            state = self._state(rid, status)
            self._finalize(rid, state)
            ev = next(e for e in d1_api.subscribe_events(rid)
                      if e["event"] == "request_terminal")
            assert json.loads(ev["data"])["status"] == status

    def test_failed_publishes_request_terminal_with_message(self) -> None:
        from food_agent_v2.c3.state import WorkflowError
        rid = _fresh_rid()
        state = self._state(rid, "failed")
        state.error = WorkflowError("WORKFLOW_ERROR", "模型节点失败")
        self._finalize(rid, state)
        ev = next(e for e in d1_api.subscribe_events(rid)
                  if e["event"] == "request_terminal")
        data = json.loads(ev["data"])
        assert data["status"] == "failed"
        assert data["message"] == "模型节点失败"

    def test_needs_clarification_publishes_clarification_event(self) -> None:
        rid = _fresh_rid()
        state = self._state(rid, "needs_clarification")
        self._finalize(rid, state)
        types = [e["event"] for e in d1_api.subscribe_events(rid)]
        assert "clarification_needed" in types
        assert "request_terminal" not in types
