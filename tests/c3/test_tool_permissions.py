"""T16 C3 角色白名单与工具回执测试。

五角色白名单静态；回执必须绑定 request/node/input（build 身份）；必需工具
漏调/失败立即失败，不自动重试。
"""

from uuid import UUID

import pytest

from food_agent_v2.c3 import ROLE_POLICIES, NodeValidator
from food_agent_v2.c3.receipts import ReceiptBindingError, ToolReceipt, validate_workflow_receipt
from food_agent_v2.c3.state import NodeType, WorkflowError, WorkflowState
from food_agent_v2.c3.tool_handler import ToolContext, ToolHandler

RID = UUID("11111111-1111-1111-1111-111111111111")
BID = UUID("22222222-2222-2222-2222-222222222222")


def make_receipt(**overrides) -> ToolReceipt:
    data = {
        "request_id": RID,
        "node_id": "query_understanding",
        "tool_call_id": "call-001",
        "tool_name": "retrieve_recipes",
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "build_id": BID,
        "success": True,
        "error_code": None,
    }
    data.update(overrides)
    return ToolReceipt(**data)


class TestRolePolicies:
    def test_five_roles_static(self) -> None:
        assert set(ROLE_POLICIES) == {
            "query_understanding",
            "health_menu_planning",
            "menu_decision",
            "answer_generation",
            "unified_review",
        }

    def test_forbidden_tool_not_in_allowed(self) -> None:
        for policy in ROLE_POLICIES.values():
            allowed = {tool.name for tool in policy.allowed_tools}
            assert not (allowed & set(policy.forbidden_tools)), f"{policy.role} 允许与禁止工具重叠"

    def test_query_understanding_forbids_b4_tools(self) -> None:
        policy = ROLE_POLICIES["query_understanding"]
        assert "validate_selected_menu_health" in policy.forbidden_tools
        assert all(t.name != "validate_selected_menu_health" for t in policy.allowed_tools)


class TestReceiptValidation:
    def test_receipt_matches_binding(self) -> None:
        receipt = make_receipt()
        validate_workflow_receipt(
            receipt,
            request_id=RID,
            node_id="query_understanding",
            input_hash="a" * 64,
            build_id=BID,
        )

    def test_receipt_reused_across_request_rejected(self) -> None:
        receipt = make_receipt()
        with pytest.raises(ReceiptBindingError) as excinfo:
            validate_workflow_receipt(
                receipt,
                request_id=UUID(int=9),
                node_id="query_understanding",
                input_hash="a" * 64,
                build_id=BID,
            )
        assert excinfo.value.code == "RECEIPT_BINDING_MISMATCH"

    def test_receipt_reused_across_node_rejected(self) -> None:
        receipt = make_receipt()
        with pytest.raises(ReceiptBindingError) as excinfo:
            validate_workflow_receipt(
                receipt,
                request_id=RID,
                node_id="menu_decision",
                input_hash="a" * 64,
                build_id=BID,
            )
        assert excinfo.value.code == "RECEIPT_BINDING_MISMATCH"

    def test_receipt_reused_across_input_rejected(self) -> None:
        receipt = make_receipt()
        with pytest.raises(ReceiptBindingError) as excinfo:
            validate_workflow_receipt(
                receipt,
                request_id=RID,
                node_id="query_understanding",
                input_hash="c" * 64,
                build_id=BID,
            )
        assert excinfo.value.code == "RECEIPT_BINDING_MISMATCH"

    def test_receipt_reused_across_build_rejected(self) -> None:
        receipt = make_receipt()
        with pytest.raises(ReceiptBindingError) as excinfo:
            validate_workflow_receipt(
                receipt,
                request_id=RID,
                node_id="query_understanding",
                input_hash="a" * 64,
                build_id=UUID(int=5),
            )
        assert excinfo.value.code == "RECEIPT_BINDING_MISMATCH"


class TestRequiredTools:
    """必需工具校验改用 menu_decision（唯一必需工具 validate_selected_menu_health）。

    query_understanding 的 retrieve_recipes 已改可选，不再有必需工具；
    安全工具的必需性由 menu_decision / health_menu_planning 覆盖。
    """

    _RECEIPT = dict(node_id="menu_decision", tool_name="validate_selected_menu_health")

    def _query_state(self) -> WorkflowState:
        state = WorkflowState(request_id=str(RID), build_id=str(BID))
        state.current_node = NodeType.MENU_DECISION
        return state

    def test_missing_required_tool_fails(self) -> None:
        error = NodeValidator.post_check(
            self._query_state(), ROLE_POLICIES["menu_decision"], {"ok": 1}, []
        )
        assert error is not None
        assert error.error_code == "REQUIRED_TOOL_NOT_CALLED"

    def test_failed_required_tool_fails(self) -> None:
        receipt = make_receipt(success=False, error_code="TOOL_EXECUTION_FAILED", **self._RECEIPT)
        error = NodeValidator.post_check(
            self._query_state(), ROLE_POLICIES["menu_decision"], {"ok": 1}, [receipt]
        )
        assert error is not None
        assert error.error_code == "TOOL_EXECUTION_FAILED"

    def test_passed_required_tool_passes(self) -> None:
        error = NodeValidator.post_check(
            self._query_state(), ROLE_POLICIES["menu_decision"], {"ok": 1},
            [make_receipt(**self._RECEIPT)]
        )
        assert error is None

    def test_query_understanding_retrieval_now_optional(self) -> None:
        """检索已改可选：query_understanding 无必需工具，漏调不失败。"""
        state = WorkflowState(request_id=str(RID), build_id=str(BID))
        state.current_node = NodeType.QUERY_UNDERSTANDING
        error = NodeValidator.post_check(
            state, ROLE_POLICIES["query_understanding"], {"ok": 1}, []
        )
        assert error is None


class TestReceiptIdentityFailClosed:
    """NodeValidator 必须验证必需工具回执的完整身份（request/node/input/build）。

    跨 request、跨 node、跨 build 或缺失身份的伪造回执一律不能通过必需工具检查。
    使用 menu_decision 必需工具 validate_selected_menu_health（安全工具仍必需）。
    """

    _RECEIPT = dict(node_id="menu_decision", tool_name="validate_selected_menu_health")

    def _query_state(self) -> WorkflowState:
        state = WorkflowState(request_id=str(RID), build_id=str(BID))
        state.current_node = NodeType.MENU_DECISION
        return state

    def _post(self, receipt) -> WorkflowError | None:
        return NodeValidator.post_check(
            self._query_state(), ROLE_POLICIES["menu_decision"], {"ok": 1}, [receipt]
        )

    def test_cross_request_forgery_rejected(self) -> None:
        error = self._post(make_receipt(request_id=UUID(int=9), **self._RECEIPT))
        assert error is not None
        assert error.error_code == "RECEIPT_BINDING_MISMATCH"

    def test_cross_node_forgery_rejected(self) -> None:
        # 伪造回执绑定到错误 node → 拒绝（本测试期望 node 是 menu_decision）
        error = self._post(make_receipt(node_id="query_understanding", tool_name="retrieve_recipes"))
        assert error is not None
        assert error.error_code == "RECEIPT_BINDING_MISMATCH"

    def test_cross_build_forgery_rejected(self) -> None:
        error = self._post(make_receipt(build_id=UUID(int=5), **self._RECEIPT))
        assert error is not None
        assert error.error_code == "RECEIPT_BINDING_MISMATCH"

    def test_zero_build_rejected(self) -> None:
        error = self._post(make_receipt(build_id=UUID(int=0)))
        assert error is not None
        assert error.error_code == "RECEIPT_BINDING_MISMATCH"

    def test_empty_build_rejected(self) -> None:
        receipt = make_receipt().model_dump()
        receipt["build_id"] = ""
        error = self._post(receipt)
        assert error is not None
        assert error.error_code == "RECEIPT_BINDING_MISMATCH"

    def test_missing_build_field_rejected(self) -> None:
        receipt = make_receipt().model_dump()
        del receipt["build_id"]
        error = self._post(receipt)
        assert error is not None
        assert error.error_code == "RECEIPT_BINDING_MISMATCH"

    def test_missing_input_hash_rejected(self) -> None:
        receipt = make_receipt().model_dump()
        receipt["input_hash"] = ""
        error = self._post(receipt)
        assert error is not None
        assert error.error_code == "RECEIPT_BINDING_MISMATCH"


class TestToolHandlerReceipts:
    """tool_handler 产生的回执必须绑定 request/node/input/build 且满足契约边界。"""

    def _bound_ctx(self) -> ToolContext:
        return ToolContext(request_id=str(RID), node_id="query_understanding", build_id=str(BID))

    def test_execute_emits_bound_receipt(self) -> None:
        handler = ToolHandler(self._bound_ctx())
        handler.execute("get_current_menu", {})
        r = handler.receipts[-1]
        assert r["request_id"] == str(RID)
        assert r["node_id"] == "query_understanding"
        assert r["build_id"] == str(BID)
        assert len(r["input_hash"]) == 64          # 契约 Sha256Hash：64 位十六进制
        assert len(r["output_hash"]) == 64
        assert r["success"] is True

    def test_bound_receipt_passes_binding_validation(self) -> None:
        handler = ToolHandler(self._bound_ctx())
        handler.execute("get_current_menu", {})
        r = handler.receipts[-1]
        validate_workflow_receipt(
            ToolReceipt(
                request_id=RID,
                node_id=r["node_id"],
                tool_call_id=r["tool_call_id"],
                tool_name=r["tool_name"],
                input_hash=r["input_hash"],
                output_hash=r["output_hash"],
                build_id=BID,
                success=r["success"],
                error_code=r["error_code"],
            ),
            request_id=RID,
            node_id=r["node_id"],
            input_hash=r["input_hash"],
            build_id=BID,
        )

    def test_same_input_same_hash_distinct_call_id(self) -> None:
        handler = ToolHandler(self._bound_ctx())
        handler.execute("get_current_menu", {})
        r1 = handler.receipts[-1]
        handler.execute("get_current_menu", {})
        r2 = handler.receipts[-1]
        assert r1["input_hash"] == r2["input_hash"]      # 相同参数 → 相同 input_hash
        assert r1["tool_call_id"] != r2["tool_call_id"]  # 每次调用独立 call id

    def test_receipts_recorded_in_request_context(self) -> None:
        ctx = self._bound_ctx()
        handler = ToolHandler(ctx)
        handler.execute("get_current_menu", {})
        assert ctx.tool_receipts == handler.receipts

    def _assert_fails_closed(self, ctx: ToolContext) -> None:
        handler = ToolHandler(ctx)
        result = handler.execute("get_current_menu", {})
        assert "error" in result                      # 工具调用失败，不返回领域结果
        receipt = handler.receipts[-1]
        assert receipt["success"] is False            # 不产生有效回执
        assert "RECEIPT_BINDING_MISMATCH" in receipt["error_code"]

    def test_missing_node_id_fails_closed(self) -> None:
        ctx = ToolContext(request_id=str(RID), build_id=str(BID))
        self._assert_fails_closed(ctx)

    def test_missing_request_id_fails_closed(self) -> None:
        ctx = ToolContext(node_id="query_understanding", build_id=str(BID))
        self._assert_fails_closed(ctx)

    def test_empty_build_fails_closed(self) -> None:
        ctx = ToolContext(request_id=str(RID), node_id="query_understanding")
        self._assert_fails_closed(ctx)

    def test_invalid_build_fails_closed(self) -> None:
        ctx = ToolContext(request_id=str(RID), node_id="query_understanding", build_id="not-a-uuid")
        self._assert_fails_closed(ctx)

    def test_zero_build_fails_closed(self) -> None:
        ctx = ToolContext(request_id=str(RID), node_id="query_understanding", build_id=str(UUID(int=0)))
        self._assert_fails_closed(ctx)
