"""C4 Redis 会话存储 —— V2 隔离（key 前缀 v2:）。"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from food_agent_v2.core.config import load_config


class RedisSessionStore:
    """Redis 会话存储（v2: 前缀隔离）。

    存储布局：
    - v2:session:{sid}:state   → SharedWorkflowContext 序列化
    - v2:session:{sid}:events  → 近期 ConversationEvent 列表
    - v2:session:{sid}:menu    → 菜单版本历史
    - v2:session:{sid}:constraints → 临时约束索引
    - v2:request:{rid}         → 请求状态缓存

    TTL: 24 小时（§20.3 决议）。
    """

    TTL_SECONDS = 86400  # 24h

    def __init__(self):
        cfg = load_config()
        self._prefix = cfg.redis.key_prefix
        self._host = cfg.redis.host
        self._port = cfg.redis.port
        self._client: Any = None

    def _connect(self):
        if self._client is not None:
            return
        try:
            import redis
            self._client = redis.Redis(
                host=self._host, port=self._port,
                decode_responses=True, socket_connect_timeout=3,
            )
            self._client.ping()
        except Exception:
            self._client = None

    def _key(self, *parts: str) -> str:
        return f"{self._prefix}:{':'.join(parts)}"

    # ---- 会话状态 ----

    def save_session_state(self, session_id: str, state: dict) -> None:
        self._connect()
        key = self._key("session", session_id, "state")
        value = json.dumps(state, ensure_ascii=False)
        if self._client:
            self._client.setex(key, self.TTL_SECONDS, value)

    def load_session_state(self, session_id: str) -> dict | None:
        self._connect()
        key = self._key("session", session_id, "state")
        if self._client:
            raw = self._client.get(key)
            if raw:
                return json.loads(raw)
        return None

    # ---- 会话事件 ----

    def save_events(self, session_id: str, events: list[dict]) -> None:
        self._connect()
        key = self._key("session", session_id, "events")
        value = json.dumps(events[-100:], ensure_ascii=False)  # 保留最近 100 条
        if self._client:
            self._client.setex(key, self.TTL_SECONDS, value)

    def load_events(self, session_id: str) -> list[dict]:
        self._connect()
        key = self._key("session", session_id, "events")
        if self._client:
            raw = self._client.get(key)
            if raw:
                return json.loads(raw)
        return []

    # ---- 菜单历史 ----

    def save_menu_history(self, session_id: str, history: list[dict]) -> None:
        self._connect()
        key = self._key("session", session_id, "menu")
        value = json.dumps(history[-5:], ensure_ascii=False)  # 保留最近 5 个
        if self._client:
            self._client.setex(key, self.TTL_SECONDS, value)

    def load_menu_history(self, session_id: str) -> list[dict]:
        self._connect()
        key = self._key("session", session_id, "menu")
        if self._client:
            raw = self._client.get(key)
            if raw:
                return json.loads(raw)
        return []

    # ---- 临时约束 ----

    def save_constraints(self, session_id: str, constraints: list[dict]) -> None:
        self._connect()
        key = self._key("session", session_id, "constraints")
        value = json.dumps(constraints, ensure_ascii=False)
        if self._client:
            self._client.setex(key, self.TTL_SECONDS, value)

    def load_constraints(self, session_id: str) -> list[dict]:
        self._connect()
        key = self._key("session", session_id, "constraints")
        if self._client:
            raw = self._client.get(key)
            if raw:
                return json.loads(raw)
        return []

    # ---- 请求状态 ----

    def save_request(self, request_id: str, state: dict) -> None:
        self._connect()
        key = self._key("request", request_id)
        value = json.dumps(state, ensure_ascii=False)
        if self._client:
            self._client.setex(key, self.TTL_SECONDS, value)

    def load_request(self, request_id: str) -> dict | None:
        self._connect()
        key = self._key("request", request_id)
        if self._client:
            raw = self._client.get(key)
            if raw:
                return json.loads(raw)
        return None

    # ---- 会话锁 ----

    def acquire_session_lock(self, session_id: str, worker_id: str) -> bool:
        """获取会话锁（防止并发请求）。"""
        self._connect()
        key = self._key("lock", "session", session_id)
        if self._client:
            return bool(self._client.set(key, worker_id, nx=True, ex=30))
        return True  # Redis 不可用时放行

    def release_session_lock(self, session_id: str, worker_id: str) -> None:
        self._connect()
        key = self._key("lock", "session", session_id)
        if self._client:
            current = self._client.get(key)
            if current == worker_id:
                self._client.delete(key)
