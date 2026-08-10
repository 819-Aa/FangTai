"""T03 工具回执 Schema 与 request/node/input 绑定测试。

回执是工具调用的一次性不可变证据，复用于不同 request/node/input 必须被拒绝；
input_hash/output_hash 必须为 64 位十六进制，禁止空串/占位。
"""

from uuid import UUID

import pytest
from pydantic import ValidationError

from food_agent_v2.contracts.receipts import (
    ReceiptBindingError,
    ToolReceipt,
    validate_receipt_binding,
)

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


class TestToolReceiptSchema:
    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ToolReceipt(**make_receipt().model_dump(), receipt_id="extra")

    def test_empty_input_hash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_receipt(input_hash="")

    def test_short_output_hash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_receipt(output_hash="abc")

    def test_failed_receipt_carries_error_code(self) -> None:
        receipt = make_receipt(success=False, error_code="TOOL_EXECUTION_FAILED")
        assert receipt.success is False
        assert receipt.error_code == "TOOL_EXECUTION_FAILED"


class TestReceiptBinding:
    def test_receipt_rejected_for_different_request(self) -> None:
        receipt = make_receipt()
        with pytest.raises(ReceiptBindingError) as excinfo:
            validate_receipt_binding(
                receipt,
                request_id=UUID(int=9),
                node_id=receipt.node_id,
                input_hash=receipt.input_hash,
                build_id=receipt.build_id,
            )
        assert excinfo.value.code == "RECEIPT_BINDING_MISMATCH"

    def test_receipt_rejected_for_different_node(self) -> None:
        receipt = make_receipt()
        with pytest.raises(ReceiptBindingError) as excinfo:
            validate_receipt_binding(
                receipt,
                request_id=receipt.request_id,
                node_id="menu_decision",
                input_hash=receipt.input_hash,
                build_id=receipt.build_id,
            )
        assert excinfo.value.code == "RECEIPT_BINDING_MISMATCH"

    def test_receipt_rejected_for_different_input(self) -> None:
        receipt = make_receipt()
        with pytest.raises(ReceiptBindingError) as excinfo:
            validate_receipt_binding(
                receipt,
                request_id=receipt.request_id,
                node_id=receipt.node_id,
                input_hash="c" * 64,
                build_id=receipt.build_id,
            )
        assert excinfo.value.code == "RECEIPT_BINDING_MISMATCH"

    def test_receipt_rejected_for_different_build(self) -> None:
        receipt = make_receipt()
        with pytest.raises(ReceiptBindingError) as excinfo:
            validate_receipt_binding(
                receipt,
                request_id=receipt.request_id,
                node_id=receipt.node_id,
                input_hash=receipt.input_hash,
                build_id=UUID(int=5),
            )
        assert excinfo.value.code == "RECEIPT_BINDING_MISMATCH"

    def test_receipt_passes_for_matching_binding(self) -> None:
        receipt = make_receipt()
        validate_receipt_binding(
            receipt,
            request_id=receipt.request_id,
            node_id=receipt.node_id,
            input_hash=receipt.input_hash,
            build_id=receipt.build_id,
        )
