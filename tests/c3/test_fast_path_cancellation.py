"""确定性快路径取消/失锁门卫测试（L0 Task 3）。

验证：取消标记在阶段边界触发 CANCELLED 终态且不提交菜单/回答；
失锁（fencing token 失效）触发 SESSION_LOCK_LOST 且不继续调用后续工具。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from food_agent_v2.c3.orchestrator import DeterministicRecommendationOrchestrator
from food_agent_v2.c4.redis_store import RedisSessionStore
from food_agent_v2.d1 import api as d1_api

BID = "22222222-2222-2222-2222-222222222222"


class _FakeC4:
    def __init__(self, lock_held: bool = True) -> None:
        self._lock_held = lock_held

    def build_shared_context(self, session_id, participant_refs, raw, mapping,
                             request_id=None, build_id=None):
        return SimpleNamespace(session_id=session_id), {}

    def validate_context_integrity(self, ref):
        return {"valid": True}

    def get_session_state(self, session_id):
        return None

    def store_temporary_constraint(self, session_id, constraint):
        pass

    def to_b4_constraints(self, session_id):
        return []

    def commit_session_state(self, request_id, status, **kw):
        pass

    def _persist_session(self, ctx):
        pass

    def is_session_lock_held(self, session_id, token):
        return self._lock_held


def _reset_d1(request_id: str) -> None:
    d1_api._requests.clear()
    d1_api._events.clear()
    d1_api._event_cursors.clear()
    d1_api._idempotency.clear()
    d1_api._requests[request_id] = {
        "request_id": request_id, "session_id": "sess_x", "status": "accepted",
        "created_at": "t", "updated_at": "t",
    }


def test_cancel_before_commit_never_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    # 取消标记始终已设置 → guard 在首个工具调用（检索）前触发 CANCELLED
    monkeypatch.setattr(
        RedisSessionStore, "is_request_cancelled", lambda self, rid: True)

    rid = str(uuid.uuid4())
    _reset_d1(rid)
    runner = DeterministicRecommendationOrchestrator(build_id=BID, c4=_FakeC4())
    runner.run(rid, "sess_x", "推荐晚餐", [{"participant_ref": "p1", "user_id": "1"}])

    s = d1_api.get_request_status(rid)[1]
    assert s["status"] == "cancelled"
    # 取消后不得提交菜单/回答（result_summary 无 completed 菜单）
    assert not s.get("result_summary") or s["result_summary"].get("status") != "completed"


def test_lost_fencing_token_stops_before_next_tool() -> None:
    # 失锁 → guard 触发 SESSION_LOCK_LOST，返回精确错误而非继续推进
    rid = str(uuid.uuid4())
    _reset_d1(rid)
    runner = DeterministicRecommendationOrchestrator(
        build_id=BID, c4=_FakeC4(lock_held=False))
    runner.run(rid, "sess_x", "推荐晚餐", [{"participant_ref": "p1", "user_id": "1"}])

    s = d1_api.get_request_status(rid)[1]
    assert s["status"] == "failed"
    assert s["error"]["code"] == "SESSION_LOCK_LOST"
