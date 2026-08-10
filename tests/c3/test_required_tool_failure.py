"""T17 必需工具失败/漏调 fail-closed，禁止节点级自动重试。

必需工具漏调 → REQUIRED_TOOL_NOT_CALLED；已调但失败 → TOOL_EXECUTION_FAILED；
模型异常 → MODEL_CALL_FAILED；同一节点重复工具调用 → WORKFLOW_RETRY_LIMIT_EXCEEDED。
全部立即失败、不重试、不模板回答。
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


class _StubLLM:
    def invoke(self, *args, **kwargs):
        raise AssertionError("_call_model 被覆盖后不应调用 LLM")


def make_state(node: NodeType = NodeType.QUERY_UNDERSTANDING) -> WorkflowState:
    state = WorkflowState(request_id=str(RID), build_id=BID)
    state.current_node = node
    return state


def make_ctx() -> ToolContext:
    return ToolContext(request_id=str(RID), node_id="query_understanding", build_id=BID)


class _ScriptedRunner(WorkflowRunner):
    """注入 _call_model 脚本结果并记录调用次数（验证无节点级自动重试）。"""

    def __init__(self, script: dict):
        super().__init__(build_id=BID, llm=_StubLLM())
        self._script = script
        self.call_count = 0

    def _call_model(self, role, policy, model_ctx, user_message, tool_ctx):
        self.call_count += 1
        if callable(self._script):
            return self._script(tool_ctx)
        return self._script


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


class TestNoNodeRetry:
    def test_missing_required_tool_no_retry(self) -> None:
        """模型漏调必需工具 → 节点立即失败，_call_model 只调用一次（无自动重试）。"""
        runner = _ScriptedRunner({"status": "ok", "content": '{"plan": 1}', "tool_calls": []})
        new_state, _ = runner._run_model_node(make_state(), _FakeC4(), make_ctx(),
                                              "query_understanding", "hi")
        assert new_state.status == RequestStatus.FAILED
        assert new_state.error is not None
        assert new_state.error.error_code == "REQUIRED_TOOL_NOT_CALLED"
        assert runner.call_count == 1

    def test_model_failure_fail_closed(self) -> None:
        """模型异常 → MODEL_CALL_FAILED，不切换模型、不模板回答。"""
        runner = _ScriptedRunner({"status": "failed", "error": "boom", "content": ""})
        new_state, _ = runner._run_model_node(make_state(), _FakeC4(), make_ctx(),
                                              "query_understanding", "hi")
        assert new_state.status == RequestStatus.FAILED
        assert new_state.error is not None
        assert new_state.error.error_code == "MODEL_CALL_FAILED"
        assert runner.call_count == 1

    def test_state_not_mutated_in_place(self) -> None:
        """reducer 返回新状态，原 state 不被原地修改（禁止直接修改 state）。"""
        original = make_state()
        runner = _ScriptedRunner({"status": "failed", "error": "boom", "content": ""})
        new_state, _ = runner._run_model_node(original, _FakeC4(), make_ctx(),
                                              "query_understanding", "hi")
        assert new_state is not original
        assert original.status == RequestStatus.ACCEPTED  # 原 state 未改
        assert new_state.status == RequestStatus.FAILED

    def test_duplicate_tool_call_budget(self) -> None:
        """同一节点内同一 (tool_name, input_hash) 出现两次 → 预算超限立即失败。"""

        def script(tool_ctx: ToolContext) -> dict:
            receipt = {
                "tool_name": "retrieve_recipes",
                "tool_call_id": "call-1",
                "request_id": str(RID),
                "node_id": "query_understanding",
                "input_hash": "a" * 64,
                "output_hash": "b" * 64,
                "build_id": BID,
                "success": True,
                "error_code": None,
                "arguments_summary": {},
                "result_summary": "{}",
            }
            tool_ctx.tool_receipts.extend([receipt, dict(receipt)])
            return {"status": "ok", "content": '{"ok": 1}', "tool_calls": []}

        runner = _ScriptedRunner(script)
        new_state, _ = runner._run_model_node(make_state(), _FakeC4(), make_ctx(),
                                              "query_understanding", "hi")
        assert new_state.status == RequestStatus.FAILED
        assert new_state.error is not None
        assert new_state.error.error_code == "WORKFLOW_RETRY_LIMIT_EXCEEDED"
