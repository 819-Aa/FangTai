"""T19 transactional outbox 集成测试（success SSE 仅由 dispatcher 在提交后发布）。

覆盖：严格 seq 顺序、稳定 event_id 幂等、崩溃可补发不重复、双 dispatcher 并发、
unknown event_type fail closed。依赖 MySQL/Redis 可用。
"""

import threading
import time
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


def _audit(rid: str, plan_id: str) -> dict:
    return {
        "request_id": rid,
        "plan_id": plan_id,
        "recipe_ids": [1],
        "menu_hash": "a" * 64,
        "final_validation": {
            "ref": "fv:1", "request_id": rid, "plan_id": plan_id,
            "menu_hash": "a" * 64, "recipe_ids": [1],
            "verdict": "PASS", "content_hash": "b" * 64,
        },
        "menu_decision": {
            "ref": "md:1", "request_id": rid, "plan_id": plan_id,
            "menu_hash": "a" * 64, "content_hash": "c" * 64,
        },
        "review": {"ref": "rv:1", "request_id": rid, "status": "PASS",
                   "content_hash": "d" * 64},
        "answer": {
            "ref": "ans:1", "request_id": rid, "plan_id": plan_id,
            "menu_hash": "a" * 64, "recipe_ids": [1], "content_hash": "e" * 64,
        },
        "participant_constraint_refs": ["p1"],
        "ingredient_relation_coverage_refs": ["ev:1"],
        "override_refs": [],
        "tool_receipt_refs": ["tc:1"],
        "tool_input_output_hashes": [
            {"tool_call_id": "tc:1", "input_hash": "f" * 64, "output_hash": "1" * 64}],
        "final_validation_verdict": "PASS",
    }


def _commit(rid: str, sid: str, token: str) -> None:
    commit_request_result(
        rid, sid, "completed", "plan-A", _audit(rid, "plan-A"),
        participant_refs=["p1"], fencing_token=token,
        answer_text="推荐菜单", menu_hash="a" * 64)


def _event_list(request_id: str) -> list[dict]:
    return d1_api.subscribe_events(request_id)


def _event_types(request_id: str) -> list[str]:
    return [e.get("event") for e in _event_list(request_id)]


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


class TestTransactionalOutbox:
    def test_dispatcher_publishes_with_stable_event_ids(self) -> None:
        _reset_d1()
        rid, sid = _unique("r"), _unique("s")
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": sid, "created_at": "t"}
        _commit(rid, sid, "3")
        assert dispatch_request(rid) == 2
        # SSE 事件 id 必须使用 outbox 稳定 event_id（不是自增 1/2）
        ids = [e.get("id") for e in _event_list(rid)]
        assert ids == [f"ev_answer_{rid}", f"ev_result_{rid}"]
        assert _event_types(rid) == ["answer_ready", "result_committed"]
        assert _outbox_status(rid) == {"dispatched": 2}

    def test_repeated_dispatch_no_duplicate_facts(self) -> None:
        _reset_d1()
        rid, sid = _unique("r"), _unique("s")
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": sid, "created_at": "t"}
        _commit(rid, sid, "4")
        assert dispatch_request(rid) == 2
        assert dispatch_request(rid) == 0
        assert _event_types(rid).count("answer_ready") == 1
        assert _event_types(rid).count("result_committed") == 1

    def test_seq_failure_stops_rest_no_dispatch(self) -> None:
        """seq=1 发布失败 → dispatch_count=0，两个事件均不得 dispatched。"""
        _reset_d1()
        rid, sid = _unique("r"), _unique("s")
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": sid, "created_at": "t"}
        _commit(rid, sid, "5")
        failing = _FailingDispatcher(fail_on="answer_ready")
        assert failing.dispatch_request(rid) == 0
        # 两个事件都不得 dispatched（seq=2 后序被禁止）
        assert _outbox_status(rid) == {"pending": 2}
        assert _event_types(rid) == []

    def test_publish_then_crash_before_mark_no_duplicate(self) -> None:
        """发布成功后、标记 dispatched 前崩溃：租约过期后恢复，不产生重复事件。"""
        _reset_d1()
        rid, sid = _unique("r"), _unique("s")
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": sid, "created_at": "t"}
        _commit(rid, sid, "6")
        crashy = _CrashAfterPublishDispatcher()
        crashy.lease_seconds = 0  # 测试：崩溃后立即可恢复
        with pytest.raises(RuntimeError):
            crashy.dispatch_request(rid)  # 发布后崩溃，行保持 dispatching
        time.sleep(1.5)  # 保证 claimed_at 早于当前秒
        # 恢复：dispatcher 补发，D1 按 event_id 去重 → 不重复事实
        recovery = OutboxDispatcher(d1_api=d1_api)
        recovery.lease_seconds = 0
        assert recovery.dispatch_request(rid) == 2
        assert _event_types(rid).count("answer_ready") == 1
        assert _event_types(rid).count("result_committed") == 1
        assert _outbox_status(rid) == {"dispatched": 2}

    def test_double_dispatcher_publish_exactly_once(self) -> None:
        """双 dispatcher 并发：底层 publisher 每个事件只调用一次（不依赖 D1 去重）。"""
        _reset_d1()
        rid, sid = _unique("r"), _unique("s")
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": sid, "created_at": "t"}
        _commit(rid, sid, "7")
        counters: list[_CountingDispatcher] = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            d = _CountingDispatcher()
            counters.append(d)
            d.dispatch_request(rid)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        # 底层 publisher 每个 event 恰好一次（claim 互斥，不靠 D1 去重掩盖）
        total = {}
        for d in counters:
            for event_id, n in d.publish_calls.items():
                total[event_id] = total.get(event_id, 0) + n
        assert total == {
            f"ev_answer_{rid}": 1,
            f"ev_result_{rid}": 1,
        }
        assert _event_types(rid).count("answer_ready") == 1
        assert _outbox_status(rid) == {"dispatched": 2}

    def test_unknown_event_type_fails_closed(self) -> None:
        _reset_d1()
        rid, sid = _unique("r"), _unique("s")
        d1_api._requests[rid] = {"request_id": rid, "status": "running",
                                 "session_id": sid, "created_at": "t"}
        _commit(rid, sid, "8")
        # 手工插入一个 unknown event_type 行（seq=3，位于已知两行之后）
        _insert_outbox(f"ev_bogus_{rid}", rid, "bogus_event", {}, 3)
        dispatched = dispatch_request(rid)
        # seq=1/2 正常发布；unknown seq=3 fail-closed：不静默标记 dispatched，保持 pending
        assert dispatched == 2
        assert _outbox_status(rid) == {"dispatched": 2, "pending": 1}


class _FailingDispatcher(OutboxDispatcher):
    def __init__(self, fail_on: str):
        super().__init__(d1_api)
        self._fail_on = fail_on

    def _publish(self, request_id: str, event_type: str, payload: dict,
                 event_id: str) -> None:
        if event_type == self._fail_on:
            raise RuntimeError("simulated crash")
        super()._publish(request_id, event_type, payload, event_id)


class _CrashAfterPublishDispatcher(OutboxDispatcher):
    """发布成功后、标记 dispatched 前崩溃（模拟进程崩溃）。"""

    def __init__(self):
        super().__init__(d1_api)
        self._crashed = False

    def _mark_dispatched(self, event_id: str, claim_token: str) -> bool:
        if not self._crashed:
            self._crashed = True
            raise RuntimeError("simulated crash after publish")
        return super()._mark_dispatched(event_id, claim_token)


class _CountingDispatcher(OutboxDispatcher):
    """记录每个 event_id 的底层 publisher 调用次数（不依赖 D1 去重）。"""

    def __init__(self):
        super().__init__(d1_api)
        self.publish_calls: dict[str, int] = {}

    def _publish(self, request_id: str, event_type: str, payload: dict,
                 event_id: str) -> None:
        self.publish_calls[event_id] = self.publish_calls.get(event_id, 0) + 1
        super()._publish(request_id, event_type, payload, event_id)


def _insert_outbox(event_id: str, request_id: str, event_type: str,
                   payload: dict, seq: int) -> None:
    import json

    import pymysql

    cfg = load_config()
    conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                           user=cfg.mysql.user, password=cfg.mysql.password,
                           database=cfg.mysql.database, charset="utf8mb4")
    cur = conn.cursor()
    cur.execute(
        "INSERT IGNORE INTO outbox (event_id, request_id, event_type, payload, seq, status) "
        "VALUES (%s, %s, %s, %s, %s, 'pending')",
        (event_id, request_id, event_type, json.dumps(payload), seq))
    conn.commit()
    conn.close()
