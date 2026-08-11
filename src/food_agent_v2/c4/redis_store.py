"""C4 Redis 会话存储 —— V2 隔离（key 前缀 v2:）。"""

from __future__ import annotations

import json
from typing import Any

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
    LOCK_TTL = 30  # 会话锁 TTL（秒），支持续租

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

    # ---- 幂等索引（跨 API 重启持久有效；T20）----

    def save_idempotency(self, key: str, record: dict) -> None:
        self._connect()
        k = self._key("idem", key)
        if self._client:
            self._client.setex(k, self.TTL_SECONDS,
                               json.dumps(record, ensure_ascii=False))

    def load_idempotency(self, key: str) -> dict | None:
        self._connect()
        k = self._key("idem", key)
        if self._client:
            raw = self._client.get(k)
            if raw:
                return json.loads(raw)
        return None

    def claim_idempotency(self, key: str, payload_hash: str,
                          request_id: str, session_id: str,
                          created_at: str, status: str) -> tuple[str, dict | None]:
        """SET NX 原子声明幂等键，并在声明时写入足够公开响应元数据。

        三态返回 (state, record)：
        - ("winner", record)：本调用赢得声明（并发下唯一），调用方创建并启动工作流；
        - ("existing", record)：键已存在，返回既有记录（即使赢家尚未写完 request state，
          也可直接按 record 构造 200 响应）；
        - ("unavailable", None)：Redis 不可用或记录不可读，调用方必须返回 503，绝不放行。
        """
        self._connect()
        k = self._key("idem", key)
        if self._client is None:
            return ("unavailable", None)
        record = {
            "payload_hash": payload_hash,
            "request_id": request_id,
            "session_id": session_id,
            "created_at": created_at,
            "status": status,
        }
        value = json.dumps(record, ensure_ascii=False)
        try:
            if self._client.set(k, value, nx=True, ex=self.TTL_SECONDS):
                return ("winner", record)
            raw = self._client.get(k)
            if raw:
                return ("existing", json.loads(raw))
        except Exception:
            return ("unavailable", None)
        return ("unavailable", None)

    # ---- 会话锁（fencing token）----

    def acquire_session_lock(self, session_id: str, worker_id: str) -> str | None:
        """获取会话锁，返回单调递增的 fencing token；未获取返回 None。

        - Redis 不可用：返回 None（不放行，fail-closed，不伪造 token）；
        - fencing token 来自全局计数器，后获取的执行者 token 更大；过期执行者
          （token 更小）不能释放或覆盖新请求持有的锁。
        """
        self._connect()
        key = self._key("lock", "session", session_id)
        if not self._client:
            return None  # Redis 不可用 → 不放行
        # 单调递增 fencing token（不复用，新锁 > 旧锁）
        token = int(self._client.incr(self._key("lock", "fencing")))
        acquired = bool(self._client.set(key, str(token), nx=True, ex=self.LOCK_TTL))
        if not acquired:
            return None
        return str(token)

    def renew_session_lock(self, session_id: str, token: str) -> bool:
        """续租会话锁 TTL（仅当 token 仍持有锁，原子 Lua）。"""
        self._connect()
        key = self._key("lock", "session", session_id)
        if not self._client:
            return False
        lua = """
        local cur = redis.call('GET', KEYS[1])
        if cur == ARGV[1] then
            return redis.call('EXPIRE', KEYS[1], ARGV[2])
        end
        return 0
        """
        result = self._client.eval(lua, 1, key, str(token), self.LOCK_TTL)
        return bool(result)

    def release_session_lock(self, session_id: str, token: str) -> bool:
        """原子释放会话锁（Lua compare-and-delete）；仅当存储 token 匹配才删除。"""
        self._connect()
        key = self._key("lock", "session", session_id)
        if not self._client:
            return False
        lua = """
        local cur = redis.call('GET', KEYS[1])
        if cur == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        result = self._client.eval(lua, 1, key, str(token))
        return bool(result)

    def is_session_lock_held_by(self, session_id: str, token: str) -> bool:
        """fencing 校验：当前锁是否仍由该 token 持有（过期执行者被拒绝）。"""
        self._connect()
        key = self._key("lock", "session", session_id)
        if not self._client:
            return False  # 无 Redis → 视为未持有（不得放行提交）
        current = self._client.get(key)
        return current is not None and str(current) == str(token)
