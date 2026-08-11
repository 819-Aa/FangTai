"""T20 SSE 续传测试：稳定字符串 event_id 精确定位，断线不丢不重，Redis 恢复续传。

覆盖：
- 稳定字符串 ID（ev_answer_xxx / ev_result_xxx）断线续传；
- 重连后不丢失、不重复事件；
- answer_ready 之后只续传 result_committed；
- Redis 恢复后继续按字符串 ID 续传；
- 重复 outbox event_id 只保留一份事实。
"""

import json
import uuid

from food_agent_v2.d1 import api


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _ready_request(rid: str) -> None:
    """直接构造 in-memory 请求（不触发工作流线程）。"""
    api._requests[rid] = {
        "request_id": rid, "session_id": "sess_x", "status": "running",
        "idempotency_key": f"ik_{rid}", "payload_hash": "x",
        "stage_events_cursor": 0, "created_at": "t",
    }


def _emit_outbox_events(rid: str) -> None:
    """模拟 outbox dispatcher：稳定字符串 event_id 发布 answer_ready + result_committed。"""
    api.publish_answer_event(rid, "推荐菜单", "menu:1", ["ev:1"],
                             event_id=f"ev_answer_{rid}")
    api.publish_result_committed(rid, {"plan_id": "plan-A", "menu_hash": "a" * 64},
                                 event_id=f"ev_result_{rid}")


def _cleanup(rid: str) -> None:
    try:
        from food_agent_v2.c4.redis_store import RedisSessionStore
        store = RedisSessionStore()
        store._connect()
        if store._client:
            store._client.delete(f"v2:request:{rid}")
    except Exception:
        pass


class TestSseReplay:
    def test_stable_string_id_resume(self) -> None:
        """Last-Event-ID=ev_answer_xxx → 只续传其后事件（不按 int 重放全部）。"""
        rid = _unique("r")
        _ready_request(rid)
        _emit_outbox_events(rid)
        tail = api.subscribe_events(rid, f"ev_answer_{rid}")
        assert [e["id"] for e in tail] == [f"ev_result_{rid}"]
        assert [e["event"] for e in tail] == ["result_committed"]
        _cleanup(rid)

    def test_reconnect_no_loss_no_duplicate(self) -> None:
        """断线重连：已收事件不重复，新事件不丢失，合并后逐条唯一。"""
        rid = _unique("r")
        _ready_request(rid)
        _emit_outbox_events(rid)
        first = api.subscribe_events(rid)
        assert [e["id"] for e in first] == [f"ev_answer_{rid}", f"ev_result_{rid}"]
        # 已全部送达 → 从最后一条续传无新事件（不重复）
        tail = api.subscribe_events(rid, first[-1]["id"])
        assert tail == []
        ids = [e["id"] for e in first] + [e["id"] for e in tail]
        assert len(ids) == len(set(ids))  # 无重复
        _cleanup(rid)

    def test_answer_ready_then_only_result_committed(self) -> None:
        """客户端已收到 answer_ready → 续传只给 result_committed。"""
        rid = _unique("r")
        _ready_request(rid)
        api.publish_answer_event(rid, "菜单", "m", ["e"], event_id=f"ev_answer_{rid}")
        api.publish_result_committed(rid, {"plan_id": "p"}, event_id=f"ev_result_{rid}")
        events = api.subscribe_events(rid, f"ev_answer_{rid}")
        assert [e["event"] for e in events] == ["result_committed"]
        payload = json.loads(events[0]["data"])
        assert payload["menu_summary"]["plan_id"] == "p"
        _cleanup(rid)

    def test_redis_recovery_continues_replay(self) -> None:
        """API 重启（内存清空）后从 Redis 恢复，仍按字符串 ID 续传。"""
        rid = _unique("r")
        _ready_request(rid)
        _emit_outbox_events(rid)
        api._persist_request(rid)
        # 模拟重启：清空内存（请求/事件/游标/幂等全部丢失）
        api._requests.clear()
        api._events.clear()
        api._event_cursors.clear()
        api._idempotency.clear()
        tail = api.subscribe_events(rid, f"ev_answer_{rid}")
        assert [e["event"] for e in tail] == ["result_committed"]
        # 幂等键也恢复
        assert api._idempotency.get(f"ik_{rid}") is not None
        _cleanup(rid)

    def test_duplicate_event_id_keeps_one_fact(self) -> None:
        """重复发布相同 outbox event_id → 只保留一份事实（幂等去重）。"""
        rid = _unique("r")
        _ready_request(rid)
        api.publish_answer_event(rid, "菜单", "m", ["e"], event_id=f"ev_answer_{rid}")
        api.publish_answer_event(rid, "菜单", "m", ["e"], event_id=f"ev_answer_{rid}")
        events = api.subscribe_events(rid)
        assert len([e for e in events if e["id"] == f"ev_answer_{rid}"]) == 1
        assert [e["event"] for e in events] == ["answer_ready"]
        _cleanup(rid)
