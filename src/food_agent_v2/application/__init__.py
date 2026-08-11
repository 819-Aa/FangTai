"""Application 提交服务（T19）—— 结果/审计/会话/outbox 原子提交的对外接口。

真实逻辑在 commit_service.py（原子事务）与 outbox.py（dispatcher）：
- `commit_request_result`：同一事务写入不可变结果、强制健康审计、会话事实、
  completed 时的有序 outbox 行，并校验单调 fencing token（stale 回滚）；
- `dispatch_request` / `OutboxDispatcher`：事务提交后发布 success SSE（幂等）。
"""

from __future__ import annotations

from food_agent_v2.application.commit_service import (
    AuditCommitFailed,
    commit_request_result,
)
from food_agent_v2.application.outbox import OutboxDispatcher, dispatch_request

__all__ = [
    "AuditCommitFailed",
    "OutboxDispatcher",
    "commit_request_result",
    "dispatch_request",
]
