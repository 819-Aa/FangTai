"""请求状态与判定的封闭枚举（T03）。

- RequestStatus：请求全生命周期状态。
- HealthVerdict / ReviewVerdict：只有确定性服务能输出的判定，未知值一律拒绝。
- Sha256Hash：所有散列字段的封闭格式（64 位十六进制）。
"""

from typing import Annotated, Literal

from pydantic import Field

RequestStatus = Literal[
    "accepted",
    "running",
    "revising",
    "completed",
    "needs_clarification",
    "no_safe_menu",
    "no_feasible_menu",
    "failed",
    "cancelled",
    "interrupted",
]

#: 仅确定性服务能输出的健康判定；未知 verdict 一律拒绝。
HealthVerdict = Literal["PASS", "EXCLUDE"]
#: 统一审查只能输出 PASS 或 REVISION_REQUIRED。
ReviewVerdict = Literal["PASS", "REVISION_REQUIRED"]

#: 全部散列字段必须为 64 位十六进制，禁止空串或占位。
Sha256Hash = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")]
