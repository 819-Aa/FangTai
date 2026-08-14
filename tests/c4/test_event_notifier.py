"""C4 事件通知器测试（L0 Task 1）。

验证：EventSubscription.wait 真正阻塞并传递 timeout；进程内 Condition 订阅
可被 publish 唤醒；subscribe_events(refresh=True) 跨 worker 从持久事件源合并。
"""

from __future__ import annotations

import threading
import time

from food_agent_v2.c4.event_notifier import (
    RequestEventNotifier,
    _InProcessSubscription,
)
from food_agent_v2.c4.redis_store import RedisSessionStore
from food_agent_v2.d1 import api


def test_in_process_subscription_blocks_until_timeout() -> None:
    sub = _InProcessSubscription(threading.Condition())
    t0 = time.perf_counter()
    sub.wait(0.2)
    assert time.perf_counter() - t0 >= 0.2


def test_in_process_subscription_woken_by_condition() -> None:
    cond = threading.Condition()
    sub = _InProcessSubscription(cond)
    elapsed: dict[str, float] = {}

    def waiter() -> None:
        t0 = time.perf_counter()
        sub.wait(5.0)
        elapsed["v"] = time.perf_counter() - t0

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    with cond:
        cond.notify_all()
    t.join(timeout=2)
    # 被唤醒，而非等满 5s
    assert elapsed["v"] < 5.0


def test_subscribe_events_refresh_merges_persisted(monkeypatch) -> None:
    rid = "r-refresh-events"
    api._events[rid] = [{"id": "1", "event": "request_accepted", "data": "{}"}]
    # 让 _restore_request 不覆盖本地（rid 已在 _requests）
    api._requests[rid] = {"request_id": rid, "status": "accepted"}
    # Redis 持久事件含本 worker 没有的 answer_ready（模拟其他 worker 写入）
    monkeypatch.setattr(
        RedisSessionStore, "load_request",
        lambda self, r: {"events": [
            {"id": "1", "event": "request_accepted", "data": "{}"},
            {"id": "ev_answer_r1", "event": "answer_ready", "data": "{}"},
        ]})

    # 不 refresh：只读本地（无 answer_ready）
    events_local = api.subscribe_events(rid, "1")
    assert [e["id"] for e in events_local] == []
    # refresh：从 Redis 合并（answer_ready 出现）
    events = api.subscribe_events(rid, "1", refresh=True)
    assert [e["id"] for e in events] == ["ev_answer_r1"]
    # 清理
    api._events.pop(rid, None)
    api._requests.pop(rid, None)


def test_notifier_publish_and_subscribe_smoke() -> None:
    notifier = RequestEventNotifier()
    notifier.publish("r1")  # 不抛异常（Redis 可用或不可用都静默）
    sub = notifier.subscribe("r1")
    assert sub is not None
    sub.close()
