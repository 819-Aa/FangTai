"""T17 必需工具失败/漏调 fail-closed，禁止节点级自动重试与漏调提示。

直接覆盖真实 `_call_model`（注入 FakeLLM，不 override）：必需工具漏调 →
REQUIRED_TOOL_NOT_CALLED；模型异常/空输出 → MODEL_CALL_FAILED；漏调不提示
补齐、不 nudge 空输出、不自动重试。
"""

from types import SimpleNamespace
from uuid import UUID

from food_agent_v2.c3 import ROLE_POLICIES, NodeValidator
from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.state import NodeType, RequestStatus, WorkflowState
from food_agent_v2.c3.tool_handler import ToolContext
from food_agent_v2.contracts.receipts import ToolReceipt

RID = UUID("11111111-1111-1111-1111-111111111111")
BID = "22222222-2222-2222-2222-222222222222"


class _FakeC4:
    """满足 runner 使用的 ContextService 接口的轻量 Fake。"""

    def __init__(self) -> None:
        self._sessions: dict = {}

    def project_model_context(self, role, handoff, ref):
        return SimpleNamespace(role=role, conversation_visible=[],
                               constraint_visible=[], menu_visible={})

    def build_shared_context(self, *args, **kwargs):
        return SimpleNamespace(session_id="sess-1"), {}

    def validate_context_integrity(self, ref):
        return {"valid": True}

    def _persist_session(self, ctx):
        pass

    def commit_session_state(self, request_id, status):
        pass


class _FakeLLM:
    """记录调用序列，按脚本返回响应（最后一个响应重复）。"""

    def __init__(self, responses: list[dict], raise_on_invoke: bool = False):
        self._responses = responses
        self._raise_on_invoke = raise_on_invoke
        self.calls: list[str] = []  # 每次调用的 user_message
        self.tool_defs: list[list[str]] = []

    def invoke(self, role, system_prompt, user_message, tools=None, response_format=None):
        if self._raise_on_invoke:
            raise RuntimeError("api down")
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append(user_message)
        self.tool_defs.append([
            item["function"]["name"] for item in (tools or [])
        ])
        return self._responses[idx]


def make_runner(responses: list[dict], raise_on_invoke: bool = False):
    llm = _FakeLLM(responses, raise_on_invoke)
    runner = WorkflowRunner(build_id=BID, llm=llm)
    return runner, llm


def make_state(node: NodeType = NodeType.QUERY_UNDERSTANDING) -> WorkflowState:
    state = WorkflowState(request_id=str(RID), build_id=BID)
    state.current_node = node
    return state


def make_ctx() -> ToolContext:
    return ToolContext(request_id=str(RID), node_id="query_understanding", build_id=BID)


def make_receipt(success: bool = True, **overrides) -> ToolReceipt:
    data = {
        "request_id": RID,
        "node_id": "query_understanding",
        "tool_call_id": "call-1",
        "tool_name": "retrieve_recipes",
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "build_id": UUID(BID),
        "success": success,
        "error_code": None,
    }
    data.update(overrides)
    return ToolReceipt(**data)


class TestNodeValidatorRequiredTools:
    def test_missing_required_tool(self) -> None:
        err = NodeValidator.post_check(
            make_state(), ROLE_POLICIES["query_understanding"], {"ok": 1}, [])
        assert err is not None
        assert err.error_code == "REQUIRED_TOOL_NOT_CALLED"

    def test_failed_required_tool(self) -> None:
        receipt = make_receipt(success=False, error_code="TOOL_EXECUTION_FAILED")
        err = NodeValidator.post_check(
            make_state(), ROLE_POLICIES["query_understanding"], {"ok": 1}, [receipt])
        assert err is not None
        assert err.error_code == "TOOL_EXECUTION_FAILED"

    def test_success_required_tool_passes(self) -> None:
        err = NodeValidator.post_check(
            make_state(), ROLE_POLICIES["query_understanding"], {"ok": 1}, [make_receipt()])
        assert err is None


class TestRealCallModelFailClosed:
    """直测真实 _call_model（FakeLLM 注入，不 override）。"""

    def test_missing_required_tool_no_retry(self) -> None:
        """模型提交最终输出但缺少必需工具回执 → 立即 REQUIRED_TOOL_NOT_CALLED，不重试。"""
        runner, llm = make_runner([{"content": '{"intent": "new"}', "tool_calls": []}])
        new_state, _raw, _art = runner._run_model_node(
            make_state(), _FakeC4(), make_ctx(), "query_understanding", "hi")
        assert new_state.status == RequestStatus.FAILED
        assert new_state.error is not None
        assert new_state.error.error_code == "REQUIRED_TOOL_NOT_CALLED"
        assert len(llm.calls) == 1  # 无节点级自动重试、无漏调提示二次调用

    def test_empty_output_fails_immediately_no_nudge(self) -> None:
        """空输出立即失败，不 nudge 重试。"""
        runner, llm = make_runner([{"content": "", "tool_calls": []}])
        new_state, _raw, _art = runner._run_model_node(
            make_state(), _FakeC4(), make_ctx(), "query_understanding", "hi")
        assert new_state.status == RequestStatus.FAILED
        assert new_state.error is not None
        assert new_state.error.error_code == "MODEL_CALL_FAILED"
        assert "MODEL_EMPTY_OUTPUT" in (new_state.error.message or "")
        assert len(llm.calls) == 1  # 无“你的输出为空”式二次提示

    def test_no_missing_tool_nudge_prompt(self) -> None:
        """漏调必需工具时不附加“你还没有调用……”提示，也不代调。"""
        runner, llm = make_runner([{"content": '{"intent": "new"}', "tool_calls": []}])
        _new_state, _raw, _art = runner._run_model_node(
            make_state(), _FakeC4(), make_ctx(), "query_understanding", "hi")
        assert len(llm.calls) == 1
        assert "你还没有调用" not in llm.calls[0]

    def test_model_exception_fails_closed(self) -> None:
        """模型 API 异常 → MODEL_CALL_FAILED，不切换模型、不模板回答。"""
        runner, llm = make_runner([], raise_on_invoke=True)
        new_state, _raw, _art = runner._run_model_node(
            make_state(), _FakeC4(), make_ctx(), "query_understanding", "hi")
        assert new_state.status == RequestStatus.FAILED
        assert new_state.error is not None
        assert new_state.error.error_code == "MODEL_CALL_FAILED"
        assert len(llm.calls) == 0

    def test_model_output_failure_marker_fails(self) -> None:
        """模型输出失败标记 → MODEL_CALL_FAILED。"""
        runner, _llm = make_runner(
            [{"content": '{"status": "failed", "error": "boom"}', "tool_calls": []}])
        new_state, _raw, _art = runner._run_model_node(
            make_state(), _FakeC4(), make_ctx(), "query_understanding", "hi")
        assert new_state.status == RequestStatus.FAILED
        assert new_state.error is not None
        assert new_state.error.error_code == "MODEL_CALL_FAILED"

    def test_state_not_mutated_in_place(self) -> None:
        """reducer 返回新状态，原 state 不被原地修改（禁止直接修改 state）。"""
        original = make_state()
        runner, _llm = make_runner([{"content": '{"intent": "new"}', "tool_calls": []}])
        new_state, _raw, _art = runner._run_model_node(
            original, _FakeC4(), make_ctx(), "query_understanding", "hi")
        assert new_state is not original
        assert original.status == RequestStatus.ACCEPTED  # 原 state 未改
        assert new_state.status == RequestStatus.FAILED

    def test_duplicate_tool_call_budget_unit(self) -> None:
        """同一节点内同一 (tool_name, input_hash) 出现两次 → 预算超限。"""
        dup = [{"tool_name": "retrieve_recipes", "input_hash": "a" * 64}] * 2
        err = WorkflowRunner._validate_tool_budget(dup)
        assert err is not None
        assert err.error_code == "WORKFLOW_RETRY_LIMIT_EXCEEDED"

    def test_preexecuted_retrieval_satisfies_query_node(self, monkeypatch) -> None:
        """Runner 可在模型前确定性检索，模型只负责语义 Artifact。"""
        from food_agent_v2.c3 import tool_handler

        semantic = {
            "flavor_preferences": ["家常"],
            "cuisine_preferences": [],
            "dish_types": [],
            "cooking_methods": [],
            "preferred_ingredients": [],
            "meal_type": None,
            "scenario": None,
            "diversity_requirements": [],
            "dish_count_requested": 4,
            "health_exclusions": [],
            "preference_exclusions": [],
            "time_constraint_seconds": 2700,
            "time_constraint_policy": "hard",
            "evidence_refs": [],
        }
        runner, llm = make_runner([
            {"content": __import__("json").dumps(semantic), "tool_calls": []}
        ])
        ctx = make_ctx()
        ctx.participant_user_mapping = {"p1": 1}
        monkeypatch.setitem(
            tool_handler._TOOL_MAP,
            "retrieve_recipes",
            lambda args, _ctx: {"total": 1, "candidates": [{"recipe_id": 7}]},
        )

        new_state, _raw, artifact = runner._run_model_node(
            make_state(),
            _FakeC4(),
            ctx,
            "query_understanding",
            "推荐三菜一汤",
            deterministic_tools=[("retrieve_recipes", {"query": "推荐三菜一汤"})],
        )

        assert new_state.status == RequestStatus.ACCEPTED
        assert artifact is not None
        assert [r.tool_name for r in new_state.tool_receipts] == ["retrieve_recipes"]
        assert "retrieve_recipes" not in llm.tool_defs[0]
        assert "已执行确定性工具" in llm.calls[0]

    def test_used_tool_is_not_offered_again_next_round(self) -> None:
        """单节点已执行工具从下一轮 tools 中移除，避免模型重复调用。"""
        runner, llm = make_runner([
            {"content": "", "tool_calls": [{"name": "get_current_menu", "arguments": {}}]},
            {"content": '{"intent": "new"}', "tool_calls": []},
        ])

        runner._call_model(
            "query_understanding",
            ROLE_POLICIES["query_understanding"],
            _FakeC4().project_model_context("query_understanding", None, "x"),
            "hi",
            make_ctx(),
        )

        assert "get_current_menu" in llm.tool_defs[0]
        assert "get_current_menu" not in llm.tool_defs[1]
