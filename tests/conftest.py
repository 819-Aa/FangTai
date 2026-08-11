"""测试基础设施：固定幂等键持久化不污染跨 pytest 调用（T20 F）。

test_invariants 的 TestD1 使用固定幂等键（t-cancel/t-persist）；幂等索引持久化到
Redis 后，跨 pytest 调用这些键会命中旧的终态请求。每个 pytest 会话开始时清理这些
固定测试键，保证测试确定性——不改动锁定测试本身。
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _clean_fixed_test_idempotency_keys():
    try:
        from food_agent_v2.c4.redis_store import RedisSessionStore
        store = RedisSessionStore()
        store._connect()
        if store._client:
            for key in ("t-cancel", "t-persist"):
                store._client.delete(f"v2:idem:{key}")
    except Exception:
        pass
    yield
