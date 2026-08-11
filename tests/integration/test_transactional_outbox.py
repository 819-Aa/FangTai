"""T19 transactional outbox 集成测试（success SSE 仅由 dispatcher 在提交后发布）。

依赖 MySQL/Redis 可用。
"""

import uuid

import pytest

from food_agent_v2.application import commit_request_result
from food_agent_v2.application.outbox import OutboxDispatcher, dispatch_request
from food_agent_v2.core.config import load_config
from food_agent_v2.d1 import api as d1_api


def _mysql_available() -> bool:
    try:
        import pymysql

        cfg = load_config()
        conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                               user=cfg.mysql.user, password=cfg.mysql.password,
                               database=cfg.mysql.database, charset="utf8mb4",
                               connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mysql_available(), reason="MySQL 不可用")


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _reset_d1() -> None:
    d1_api._requests.clear()
    d1_api._events.clear()
    d1_api._event_cursors.clear()
    d1_api._idempotency.clear()


def _event_types(request_id: str) -> list[str]:
    return [e.get("event") for e in d1_api.subscribe_events(request_id)]


class TestTransactionalOutbox:
    def test_dispatcher_publishes_success_after_commit(self) -> None:
        _reset_d1()
        rid, sid = _unique("r"), _unique("s")
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": sid, "created_at": "t"}
        commit_request_result(
            rid, sid, "completed", "plan-A", {"recipe_ids": [1], "verdict": "PASS"},
            participant_refs=["p1"], fencing_token="3",
            answer_text="推荐菜单", menu_hash="a" * 64)
        # 提交后 dispatcher 发布 success SSE
        dispatched = dispatch_request(rid)
        assert dispatched == 2
        types = _event_types(rid)
        assert "answer_ready" in types
        assert "result_committed" in types
        # outbox 已标记 dispatched
        assert _outbox_status(rid) == {"dispatched": 2}

    def test_repeated_dispatch_no_duplicate_facts(self) -> None:
        _reset_d1()
        rid, sid = _unique("r"), _unique("s")
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": sid, "created_at": "t"}
        commit_request_result(
            rid, sid, "completed", "plan-A", {"recipe_ids": [1], "verdict": "PASS"},
            participant_refs=["p1"], fencing_token="4",
            answer_text="推荐", menu_hash="a" * 64)
        assert dispatch_request(rid) == 2
        # 重复投递不重复事实：第二次 dispatch 0，事件数不翻倍
        assert dispatch_request(rid) == 0
        assert _event_types(rid).count("answer_ready") == 1
        assert _event_types(rid).count("result_committed") == 1

    def test_dispatcher_crash_redispatch(self) -> None:
        _reset_d1()
        rid, sid = _unique("r"), _unique("s")
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": sid, "created_at": "t"}
        commit_request_result(
            rid, sid, "completed", "plan-A", {"recipe_ids": [1], "verdict": "PASS"},
            participant_refs=["p1"], fencing_token="5",
            answer_text="推荐", menu_hash="a" * 64)
        # 模拟 dispatcher 对 answer_ready 发布失败 → 该行保持 pending → 后续可补发
        failing = _FailingDispatcher(fail_on="answer_ready")
        assert failing.dispatch_request(rid) == 1  # result_committed 已发布
        assert _outbox_status(rid) == {"pending": 1, "dispatched": 1}
        # 正常 dispatcher 补发剩余的 answer_ready
        assert dispatch_request(rid) == 1
        assert _outbox_status(rid) == {"dispatched": 2}
        # 补发后不重复事实
        assert _event_types(rid).count("answer_ready") == 1


class _FailingDispatcher(OutboxDispatcher):
    """发布指定事件类型时抛错，模拟 dispatcher 中途崩溃。"""

    def __init__(self, fail_on: str):
        super().__init__(d1_api)
        self._fail_on = fail_on

    def _publish(self, request_id: str, event_type: str, payload: dict) -> None:
        if event_type == self._fail_on:
            raise RuntimeError("simulated crash")
        super()._publish(request_id, event_type, payload)


def _outbox_status(request_id: str) -> dict:
    import pymysql

    cfg = load_config()
    conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                           user=cfg.mysql.user, password=cfg.mysql.password,
                           database=cfg.mysql.database, charset="utf8mb4")
    cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) FROM outbox WHERE request_id=%s "
                "GROUP BY status", (request_id,))
    result = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return result
