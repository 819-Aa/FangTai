"""测试基础设施：固定幂等键持久化不污染跨 pytest 调用（T20 F）。

test_invariants 的 TestD1 使用固定幂等键（t-cancel/t-persist）；幂等索引持久化到
Redis 后，跨 pytest 调用这些键会命中旧的终态请求。每个 pytest 会话开始前与结束后
清理这些固定测试键及其关联的 request key，保证测试确定性——不改动锁定测试本身。
"""

import json

import pytest

_FIXED_TEST_KEYS = ("t-cancel", "t-persist")


def _clean_fixed_keys() -> None:
    try:
        from food_agent_v2.c4.redis_store import RedisSessionStore
        store = RedisSessionStore()
        store._connect()
        if store._client is None:
            return
        for key in _FIXED_TEST_KEYS:
            idem_key = f"v2:idem:{key}"
            raw = store._client.get(idem_key)
            store._client.delete(idem_key)
            if raw:
                try:
                    rid = json.loads(raw).get("request_id")
                except Exception:
                    rid = None
                if rid:
                    store._client.delete(f"v2:request:{rid}")
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _clean_fixed_test_keys():
    _clean_fixed_keys()
    yield
    _clean_fixed_keys()
