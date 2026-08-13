"""T17 必需工具失败/漏调 fail-closed，禁止节点级自动重试与漏调提示。

直接覆盖真实 `_call_model`（注入 FakeLLM，不 override）：必需工具漏调 →
REQUIRED_TOOL_NOT_CALLED；模型异常/空输出 → MODEL_CALL_FAILED；漏调不提示
补齐、不 nudge 空输出、不自动重试。
"""

from copy import deepcopy
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
        self.system_prompts: list[str] = []
        self.tool_defs: list[list[str]] = []

    def invoke(self, role, system_prompt, user_message, tools=None, response_format=None):
        if self._raise_on_invoke:
            raise RuntimeError("api down")
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append(user_message)
        self.system_prompts.append(system_prompt)
        self.tool_defs.append([
            item["function"]["name"] for item in (tools or [])
        ])
        return self._responses[idx]


class _ConversationLLM:
    """仅实现生产多轮工具协议，确保 Runner 不退回文本拼接。"""

    def __init__(self, responses: list[dict]):
        self._responses = responses
        self.calls: list[list[dict]] = []

    def invoke_messages(
        self, role, system_prompt, messages, tools=None, response_format=None,
    ):
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append(deepcopy(messages))
        return self._responses[idx]

    def invoke(self, *args, **kwargs):
        raise AssertionError("生产 Runner 不应把工具结果退化为普通 user_message")


def make_runner(responses: list[dict], raise_on_invoke: bool = False):
    llm = _FakeLLM(responses, raise_on_invoke)
    runner = WorkflowRunner(build_id=BID, llm=llm)
    return runner, llm


def make_state(node: NodeType = NodeType.HEALTH_MENU_PLANNING) -> WorkflowState:
    state = WorkflowState(request_id=str(RID), build_id=BID)
    state.current_node = node
    return state


def make_ctx() -> ToolContext:
    # 必需工具校验改用 health_menu_planning（检索在 query_understanding 已改可选，
    # 但健康审查/菜单生成等安全工具仍必需，NodeValidator 覆盖它们）。
    return ToolContext(request_id=str(RID), node_id="health_menu_planning", build_id=BID)


def make_receipt(success: bool = True, **overrides) -> ToolReceipt:
    data = {
        "request_id": RID,
        "node_id": "health_menu_planning",
        "tool_call_id": "call-1",
        "tool_name": "evaluate_recipe_health",
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "build_id": UUID(BID),
        "success": success,
        "error_code": None,
    }
    data.update(overrides)
    return ToolReceipt(**data)


class TestNodeValidatorRequiredTools:
    """用 menu_decision（唯一必需工具 validate_selected_menu_health）验证必需工具机制。"""

    _RECEIPT = dict(node_id="menu_decision", tool_name="validate_selected_menu_health")

    def test_missing_required_tool(self) -> None:
        # menu_decision 漏调必需安全工具（validate_selected_menu_health）→ 失败
        err = NodeValidator.post_check(
            make_state(NodeType.MENU_DECISION),
            ROLE_POLICIES["menu_decision"], {"ok": 1}, [])
        assert err is not None
        assert err.error_code == "REQUIRED_TOOL_NOT_CALLED"

    def test_failed_required_tool(self) -> None:
        receipt = make_receipt(success=False, error_code="TOOL_EXECUTION_FAILED", **self._RECEIPT)
        err = NodeValidator.post_check(
            make_state(NodeType.MENU_DECISION),
            ROLE_POLICIES["menu_decision"], {"ok": 1}, [receipt])
        assert err is not None
        assert err.error_code == "TOOL_EXECUTION_FAILED"

    def test_success_required_tool_passes(self) -> None:
        err = NodeValidator.post_check(
            make_state(NodeType.MENU_DECISION),
            ROLE_POLICIES["menu_decision"], {"ok": 1}, [make_receipt(**self._RECEIPT)])
        assert err is None

    def test_query_understanding_retrieval_now_optional(self) -> None:
        """检索已改可选：query_understanding 漏调 retrieve_recipes 不再失败。"""
        err = NodeValidator.post_check(
            make_state(NodeType.QUERY_UNDERSTANDING),
            ROLE_POLICIES["query_understanding"], {"ok": 1}, [])
        assert err is None


class TestRealCallModelFailClosed:
    """直测真实 _call_model（FakeLLM 注入，不 override）。"""

    def test_missing_required_tool_no_retry(self) -> None:
        """模型提交最终输出但缺少必需安全工具回执 → 立即 REQUIRED_TOOL_NOT_CALLED，不重试。

        menu_decision 仍必需 validate_selected_menu_health；补上其前置 Artifact
        使 pre_check 通过，随后必需工具漏调 → REQUIRED_TOOL_NOT_CALLED。
        """
        from food_agent_v2.c3.state import reduce_workflow_state
        state = make_state(NodeType.MENU_DECISION)
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="health_evaluation", value={"_ok": 1})
        state = reduce_workflow_state(
            state, action="set_artifact", artifact="feasible_menu", value={"_ok": 1})

        runner, llm = make_runner([{"content": '{"intent": "new"}', "tool_calls": []}])
        new_state, _raw, _art = runner._run_model_node(
            state,
            _FakeC4(),
            ToolContext(request_id=str(RID), node_id="menu_decision", build_id=BID),
            "menu_decision", "hi")
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

    def test_tool_result_continues_with_assistant_and_tool_messages(self) -> None:
        """下一轮必须保留 assistant tool_call 与同 id 的 tool result。"""
        llm = _ConversationLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-current-menu",
                    "name": "get_current_menu",
                    "arguments": {},
                }],
            },
            {"content": '{"intent": "new"}', "tool_calls": []},
        ])
        runner = WorkflowRunner(build_id=BID, llm=llm)

        result = runner._call_model(
            "query_understanding",
            ROLE_POLICIES["query_understanding"],
            _FakeC4().project_model_context("query_understanding", None, "x"),
            "hi",
            make_ctx(),
        )

        assert result == {"intent": "new"}
        assert len(llm.calls) == 2
        continuation = llm.calls[1]
        assert [message["role"] for message in continuation] == [
            "user", "assistant", "tool",
        ]
        assert continuation[1]["tool_calls"][0]["id"] == "call-current-menu"
        assert continuation[2]["tool_call_id"] == "call-current-menu"

    def test_required_receipts_are_declared_as_completion_contract(self) -> None:
        """策略门槛应显式进入提示，但 Runner 不应代模型执行工具。

        menu_decision 仍必需 validate_selected_menu_health → prompt 含节点完成契约；
        query_understanding 检索已改可选 → 无必需工具，prompt 不再追加完成契约。
        """
        runner, llm = make_runner([
            {"content": '{"plan_id": "p1"}', "tool_calls": []},
        ])
        ctx = ToolContext(request_id=str(RID), node_id="menu_decision", build_id=BID)
        state = WorkflowState(request_id=str(RID), build_id=BID)
        state.current_node = NodeType.MENU_DECISION

        runner._call_model(
            "menu_decision",
            ROLE_POLICIES["menu_decision"],
            _FakeC4().project_model_context("menu_decision", None, "x"),
            "hi",
            ctx,
        )
        prompt = llm.system_prompts[0]
        assert "节点完成契约" in prompt
        assert "validate_selected_menu_health" in prompt
        assert "由你决定何时发起" in prompt
        assert ctx.tool_receipts == []

    def test_query_understanding_no_required_contract(self) -> None:
        """检索可选后，query_understanding prompt 不再附加节点完成契约。"""
        runner, llm = make_runner([{"content": '{"intent": "new"}', "tool_calls": []}])
        ctx = make_ctx()

        runner._call_model(
            "query_understanding",
            ROLE_POLICIES["query_understanding"],
            _FakeC4().project_model_context("query_understanding", None, "x"),
            "hi",
            ctx,
        )
        prompt = llm.system_prompts[0]
        assert "节点完成契约" not in prompt
