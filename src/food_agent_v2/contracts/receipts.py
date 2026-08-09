"""工具回执契约（T03）。

每个工具调用只产生一条绑定 request_id/node_id/tool_call_id/input_hash 的
不可变回执。回执复用于不同 request/node/input 必须被拒绝
（RECEIPT_BINDING_MISMATCH）；input_hash/output_hash 必须为 64 位十六进制。
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from food_agent_v2.contracts.status import Sha256Hash


class ToolReceipt(BaseModel):
    """一次工具调用的不可变证据。"""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    node_id: str
    tool_call_id: str
    tool_name: str
    input_hash: Sha256Hash
    output_hash: Sha256Hash
    build_id: UUID
    success: bool
    error_code: str | None = None


class ReceiptBindingError(Exception):
    """回执与当前 request/node/input 绑定不一致。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def validate_receipt_binding(
    receipt: ToolReceipt,
    *,
    request_id: UUID,
    node_id: str,
    input_hash: str,
) -> None:
    """回执只能用于其绑定的 request/node/input。任一不一致即拒绝。"""
    mismatches: list[str] = []
    if receipt.request_id != request_id:
        mismatches.append("request_id")
    if receipt.node_id != node_id:
        mismatches.append("node_id")
    if receipt.input_hash != input_hash:
        mismatches.append("input_hash")
    if mismatches:
        raise ReceiptBindingError(
            "RECEIPT_BINDING_MISMATCH",
            f"回执复用于不同 request/node/input: {mismatches}",
        )
