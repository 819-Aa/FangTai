"""C3 工具回执边界（T16）。

复用 contracts/receipts.py 的 ToolReceipt 与 validate_receipt_binding：
每个回执必须绑定当前 request_id/node_id/input_hash（build 身份由
ToolReceipt.build_id 承载）。回执复用/身份不一致立即拒绝（RECEIPT_BINDING_MISMATCH）。
"""

from __future__ import annotations

from food_agent_v2.contracts.receipts import (
    ReceiptBindingError,
    ToolReceipt,
    validate_receipt_binding,
)


def validate_workflow_receipt(
    receipt: ToolReceipt,
    *,
    request_id,
    node_id: str,
    input_hash: str,
) -> None:
    """回执必须绑定当前 request/node/input；不一致即抛 ReceiptBindingError。"""
    validate_receipt_binding(
        receipt,
        request_id=request_id,
        node_id=node_id,
        input_hash=input_hash,
    )


__all__ = [
    "ReceiptBindingError",
    "ToolReceipt",
    "validate_receipt_binding",
    "validate_workflow_receipt",
]
