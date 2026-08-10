"""T18 C4 会话锁 fencing token 与 MySQL+Redis 组合恢复集成测试。

依赖 MySQL/Redis 可用；不可用时跳过。
"""

import time
import uuid

import pytest

from food_agent_v2.core.config import load_config


def _redis_available() -> bool:
    try:
        from food_agent_v2.c4.redis_store import RedisSessionStore
        store = RedisSessionStore()
        store._connect()
        return store._client is not None
    except Exception:
        return False


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


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.mark.skipif(not _redis_available(), reason="Redis 不可用")
class TestSessionLockFencing:
    def test_fencing_token_acquire_release(self) -> None:
        from food_agent_v2.c4.redis_store import RedisSessionStore

        store = RedisSessionStore()
        sid = _unique("lock")
        t1 = store.acquire_session_lock(sid, "worker1")
        assert t1 is not None
        # 锁被持有 → 第二次获取失败
        assert store.acquire_session_lock(sid, "worker2") is None
        # 过期执行者（错误 token）不能释放新锁
        assert store.release_session_lock(sid, "999999") is False
        assert store.is_session_lock_held_by(sid, t1) is True
        # 正确 token 释放
        assert store.release_session_lock(sid, t1) is True

    def test_lock_renewal_extends_ttl(self) -> None:
        from food_agent_v2.c4.redis_store import RedisSessionStore

        store = RedisSessionStore()
        sid = _unique("renew")
        token = store.acquire_session_lock(sid, "worker1")
        assert token is not None
        assert store.renew_session_lock(sid, token) is True
        assert store.is_session_lock_held_by(sid, token) is True
        # 旧 token 续租失败
        assert store.renew_session_lock(sid, "999") is False
        store.release_session_lock(sid, token)

    def test_redis_down_acquire_returns_none(self) -> None:
        from food_agent_v2.c4.redis_store import RedisSessionStore

        store = RedisSessionStore()
        # 模拟 Redis 不可用：不可达端口 → _connect 失败 → _client=None
        store._client = None
        store._host = "127.0.0.1"
        store._port = 1
        assert store.acquire_session_lock(_unique("down"), "w") is None
        assert store.release_session_lock(_unique("down"), "x") is False
        assert store.is_session_lock_held_by(_unique("down"), "x") is False

    def test_second_executor_cannot_acquire_beyond_ttl_with_heartbeat(self) -> None:
        """单节点执行时间超过 TTL：第一执行者 heartbeat 续租，第二执行者始终不能获得锁。"""
        from food_agent_v2.c4.redis_store import RedisSessionStore

        store = RedisSessionStore()
        store.LOCK_TTL = 1  # 缩短 TTL 模拟单节点执行超 TTL
        sid = _unique("beyond")
        t1 = store.acquire_session_lock(sid, "w1")
        assert t1 is not None
        for _ in range(3):
            time.sleep(0.5)  # 累计超过原始 TTL=1s
            # heartbeat 续租 → 锁保持
            assert store.renew_session_lock(sid, t1) is True
            assert store.is_session_lock_held_by(sid, t1) is True
            # 第二执行者始终拿不到锁
            assert store.acquire_session_lock(sid, "w2") is None
        store.release_session_lock(sid, t1)
        # 释放后第二执行者可获取
        t2 = store.acquire_session_lock(sid, "w2")
        assert t2 is not None
        store.release_session_lock(sid, t2)

    def test_stale_token_cannot_release_or_hold(self) -> None:
        from food_agent_v2.c4.redis_store import RedisSessionStore

        store = RedisSessionStore()
        sid = _unique("stale")
        t1 = store.acquire_session_lock(sid, "w1")
        assert t1 is not None
        assert store.release_session_lock(sid, t1) is True  # 释放后
        # stale token 不再持有 → 不能提交成功结果
        assert store.is_session_lock_held_by(sid, t1) is False

    def test_fencing_token_monotonic_and_stale_rejected(self) -> None:
        from food_agent_v2.c4.redis_store import RedisSessionStore

        store = RedisSessionStore()
        sid = _unique("fence")
        t1 = store.acquire_session_lock(sid, "worker1")
        store.release_session_lock(sid, t1)
        t2 = store.acquire_session_lock(sid, "worker2")
        assert t2 is not None
        assert int(t2) > int(t1)  # 单调递增 fencing token
        # 过期执行者 t1 不能覆盖 t2 持有的锁
        assert store.is_session_lock_held_by(sid, t2) is True
        assert store.is_session_lock_held_by(sid, t1) is False
        assert store.release_session_lock(sid, t1) is False  # t1 释放失败
        store.release_session_lock(sid, t2)


@pytest.mark.skipif(not _mysql_available(), reason="MySQL 不可用")
class TestRestoreFromMySQLCommitted:
    def test_restore_committed_session_after_redis_loss(self) -> None:
        from food_agent_v2.application import commit_request_result
        from food_agent_v2.c4 import ContextService

        sid = _unique("restore")
        rid = _unique("rid")
        commit_request_result(
            request_id=rid, session_id=sid, status="completed",
            final_plan_id="plan-A",
            health_evidence={"recipe_ids": [1, 2], "menu_hash": "a" * 64},
            participant_refs=["p1"],
        )
        # 全新 ContextService（Redis 无该会话运行快照）→ 从 MySQL 已提交边界恢复
        svc = ContextService()
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "恢复请求"}, {"p1": 1}, request_id=_unique("rid2"))
        assert any(v.get("plan_id") == "plan-A" for v in ctx.menu_history), \
            "应从 MySQL menu_versions 恢复已提交菜单"
