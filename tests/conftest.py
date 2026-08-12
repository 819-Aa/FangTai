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


# ---- MC-02：自包含的 50 份合成固定档案（不依赖 data/ 或 MySQL）----

MC02_TEST_READY_BUILD = "3b8a9c2e-4d1f-4a90-9e5b-7a2f6c8d0e11"


def make_mc02_user_records(build_id: str = MC02_TEST_READY_BUILD) -> list[dict]:
    """生成 50 份封闭注册表全合法的合成档案（含 1 名备孕），供 InMemory source 使用。

    所有过敏/疾病映射到批准代码；指标均为正常值；uid=5 含备孕特殊阶段
    （已知但无批准代码 → 待澄清信号，不产生硬约束）。
    """
    records: list[dict] = []
    for uid in range(1, 51):
        gender = "男" if uid % 2 else "女"
        allergies = ["海鲜"] if uid % 2 else ["花生"]
        special_group = ["备孕"] if uid == 5 else None
        metrics = {
            "收缩压": {"value": "120/80"},
            "空腹血糖": {"value": 5.0},
            "总胆固醇": {"value": 4.5},
            "尿酸": {"value": 300},
        } if uid <= 5 else {}
        records.append({
            "user_id": uid,
            "source_user_id": uid,
            "gender": gender,
            "age": 30 + (uid % 5),
            "activity_level": "中",
            "special_group": special_group,
            "height_cm": None,
            "weight_kg": None,
            "bmi": 22.0,
            "dietary_preferences": ["偏清淡"],
            "allergies": allergies,
            "health_goals": [],
            "diseases": ["高血压"],
            "taboo_ingredients": [],
            "health_metrics": metrics,
            "parse_quality": {},
            "cleaning_notes": [],
            "build_id": build_id,
        })
    return records


@pytest.fixture(scope="module")
def user_records() -> list[dict]:
    """50 份合成档案记录（测试内可变，违约用例自行拷贝后修改）。"""
    return make_mc02_user_records()


@pytest.fixture(scope="module")
def user_profiles_source(user_records: list[dict]):
    """InMemoryUserProfileSource：单元测试的 B2 固定档案来源。"""
    from food_agent_v2.b2.repository import InMemoryUserProfileSource

    return InMemoryUserProfileSource(list(user_records),
                                     ready_builds=[MC02_TEST_READY_BUILD])
