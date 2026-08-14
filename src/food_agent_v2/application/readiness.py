"""只读运行就绪门禁。

``/health`` 只回答进程存活；本模块验证在线请求真正依赖的固定数据、Redis 与
Qdrant，并且只返回稳定的公开状态，不向 HTTP 层传播基础设施异常文本。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from food_agent_v2.core.config import load_config

EXPECTED_FIXED_ARTIFACT_COUNTS: dict[str, int] = {
    "health_relation_coverage": 38,
    "health_relation_decisions": 65854,
    "health_relations": 985,
    "ingredient_aliases": 21,
    "ingredient_crosswalk": 431,
    "ingredient_forms": 381,
    "ingredient_occurrences": 17521,
    "ingredient_registry": 1781,
    "nutrition_features": 1914,
    "rag_documents": 1914,
    "recipe_classifications": 2000,
    "recipe_health_views": 1914,
    "recipe_ingredient_relations": 17505,
    "recipe_nutrition_input_views": 1914,
    "recipe_retrieval_build_views": 1914,
    "recipe_source_rows": 2000,
    "recipe_step_binding_views": 1914,
    "step_tasks": 1914,
    "user_profiles": 50,
}


class ServiceNotReady(RuntimeError):
    """至少一个在线依赖未满足固定数据契约。"""

    code = "SERVICE_NOT_READY"

    def __init__(self, *, checks: dict[str, dict[str, Any]]) -> None:
        self.checks = checks
        super().__init__(self.code)


def mysql_fixed_data_probe() -> dict[str, Any]:
    """核对唯一 ready 构建及其完整的 19 类固定产物。"""
    import pymysql

    cfg = load_config().mysql
    connection = None
    cursor = None
    try:
        connection = pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=5,
            read_timeout=10,
        )
        cursor = connection.cursor()
        cursor.execute("SELECT build_id FROM data_builds WHERE status='ready'")
        ready_rows = cursor.fetchall()
        if len(ready_rows) != 1:
            raise RuntimeError("unique ready build required")
        build_id = str(ready_rows[0][0])
        if not build_id:
            raise RuntimeError("ready build identity required")

        cursor.execute(
            "SELECT artifact_name, COUNT(*) FROM fixed_artifact_records "
            "WHERE build_id=%s GROUP BY artifact_name",
            (build_id,),
        )
        actual = {str(name): int(count) for name, count in cursor.fetchall()}
        if actual != EXPECTED_FIXED_ARTIFACT_COUNTS:
            raise RuntimeError("fixed artifact counts do not match approved manifest")
        return {
            "status": "ready",
            "build_id": build_id,
            "artifact_count": len(actual),
            "recipe_count": actual["recipe_retrieval_build_views"],
        }
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


def redis_probe() -> dict[str, Any]:
    from food_agent_v2.c4.redis_store import RedisSessionStore

    RedisSessionStore().assert_ready()
    return {"status": "ready"}


def qdrant_probe(build_id: str, expected_count: int) -> dict[str, Any]:
    from food_agent_v2.c1.qdrant_client import QdrantVectorStore

    store = QdrantVectorStore()
    try:
        point_count = store.assert_ready_build(
            build_id=build_id,
            expected_count=expected_count,
        )
        return {"status": "ready", "point_count": point_count}
    finally:
        store.close()


def siliconflow_probe() -> dict[str, Any]:
    """核对 SiliconFlow embedding/rerank warmup 探测状态（只返回脱敏模型身份）。"""
    from food_agent_v2.c1.siliconflow import warmup_status

    status = warmup_status()
    if not status:
        return {"status": "unavailable"}
    models = {k: v.get("model", "") for k, v in status.items()}
    if any(s.get("status") != "ready" for s in status.values()):
        return {"status": "unavailable", "models": models}
    return {"status": "ready", "models": models}


def check_readiness(
    *,
    mysql_probe: Callable[[], dict[str, Any]] = mysql_fixed_data_probe,
    redis_probe: Callable[[], dict[str, Any]] = redis_probe,
    qdrant_probe: Callable[[str, int], dict[str, Any]] = qdrant_probe,
    siliconflow_probe: Callable[[], dict[str, Any]] = siliconflow_probe,
) -> dict[str, Any]:
    """执行三项只读探针；任一失败都以稳定、脱敏状态 fail-closed。"""
    checks: dict[str, dict[str, Any]] = {}
    build_id: str | None = None
    recipe_count: int | None = None

    try:
        mysql = mysql_probe()
        build_id = str(mysql["build_id"])
        recipe_count = int(mysql["recipe_count"])
        if not build_id or recipe_count != EXPECTED_FIXED_ARTIFACT_COUNTS[
            "recipe_retrieval_build_views"
        ]:
            raise RuntimeError("invalid fixed data identity")
        checks["mysql"] = {
            "status": "ready",
            "artifact_count": int(mysql["artifact_count"]),
            "recipe_count": recipe_count,
        }
    except Exception:
        checks["mysql"] = {"status": "unavailable"}

    try:
        redis_probe()
        checks["redis"] = {"status": "ready"}
    except Exception:
        checks["redis"] = {"status": "unavailable"}

    if build_id is None or recipe_count is None:
        checks["qdrant"] = {"status": "unavailable"}
    else:
        try:
            qdrant = qdrant_probe(build_id, recipe_count)
            if int(qdrant["point_count"]) != recipe_count:
                raise RuntimeError("qdrant point count mismatch")
            checks["qdrant"] = {"status": "ready", "point_count": recipe_count}
        except Exception:
            checks["qdrant"] = {"status": "unavailable"}

    try:
        siliconflow = siliconflow_probe()
        if siliconflow.get("status") != "ready":
            raise RuntimeError("siliconflow not ready")
        checks["siliconflow"] = siliconflow
    except Exception:
        checks["siliconflow"] = {"status": "unavailable"}

    if any(check["status"] != "ready" for check in checks.values()):
        raise ServiceNotReady(checks=checks)
    return {"status": "ready", "build_id": build_id, "checks": checks}
