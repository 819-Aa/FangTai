"""C4 请求事件即时通知 —— 公共接口，封装 Redis Pub/Sub 与进程内回退。

api_app 与 D1 不得直接访问 ``RedisSessionStore._client/_connect/_key``；统一经
本模块的 ``RequestEventNotifier``。多进程用 Redis Pub/Sub（进程级连接池），
Redis 不可用时回退进程内 ``threading.Condition``（单进程仍即时唤醒）。

发布顺序由调用方保证：持久事件写入成功后再 ``publish``；订阅者在 ``wait``
返回后从持久事件源（Redis）刷新读取，不依赖通知携带事件内容。
"""

from __future__ import annotations

import threading
from typing import Protocol


class EventSubscription(Protocol):
    """事件订阅句柄：阻塞等待通知；用完必须 close。"""

    def wait(self, timeout_seconds: float) -> bool: ...

    def close(self) -> None: ...


class _RedisSubscription:
    """Redis Pub/Sub 订阅（多进程）。"""

    def __init__(self, pubsub) -> None:
        self._pubsub = pubsub

    def wait(self, timeout_seconds: float) -> bool:
        # get_message(timeout) 真正阻塞；返回非 None 表示收到通知。
        return self._pubsub.get_message(timeout=timeout_seconds) is not None

    def close(self) -> None:
        try:
            self._pubsub.close()
        except Exception:
            pass


class _InProcessSubscription:
    """进程内 Condition 订阅（Redis 不可用时的单进程回退）。"""

    def __init__(self, condition: threading.Condition) -> None:
        self._condition = condition

    def wait(self, timeout_seconds: float) -> bool:
        with self._condition:
            self._condition.wait(timeout_seconds)
        return True

    def close(self) -> None:
        pass


class RequestEventNotifier:
    """请求事件通知器（进程级单例，线程安全）。

    publish 先唤醒进程内 Condition（单进程即时），再尽力 publish 到 Redis
    （多进程）；subscribe 优先返回 Redis 订阅，Redis 不可用回退进程内订阅。
    """

    _instance: RequestEventNotifier | None = None

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._store = None
        self._redis = None
        self._redis_checked = False

    @classmethod
    def get(cls) -> RequestEventNotifier:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_redis(self):
        if not self._redis_checked:
            self._redis_checked = True
            try:
                from food_agent_v2.c4.redis_store import RedisSessionStore
                self._store = RedisSessionStore()
                self._store._connect()
                self._redis = self._store._client
            except Exception:
                self._redis = None
        return self._redis

    def _channel(self, request_id: str) -> str:
        return self._store._key("sse", request_id)

    def publish(self, request_id: str) -> None:
        # 进程内总是唤醒（单进程即时）
        with self._condition:
            self._condition.notify_all()
        # Redis 多进程通知（尽力而为）
        redis = self._ensure_redis()
        if redis is not None:
            try:
                redis.publish(self._channel(request_id), "1")
            except Exception:
                pass

    def subscribe(self, request_id: str) -> EventSubscription:
        redis = self._ensure_redis()
        if redis is not None:
            try:
                pubsub = redis.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(self._channel(request_id))
                return _RedisSubscription(pubsub)
            except Exception:
                pass
        return _InProcessSubscription(self._condition)


def get_event_notifier() -> RequestEventNotifier:
    return RequestEventNotifier.get()
