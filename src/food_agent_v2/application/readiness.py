"""只读运行就绪门禁。

``/health`` 只回答进程存活；本模块验证在线请求真正依赖的固定数据、Redis 与
Qdrant，并且只返回稳定的公开状态，不向 HTTP 层传播基础设施异常文本。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from food_agent_v2.contracts.build import FIXED_ARTIFACT_NAMES
from food_agent_v2.core.config import load_config

EXPECTED_RUNTIME_SCHEMA_VERSIONS: dict[str, str] = {
    "rag_documents": "2.0.0",
    "nutrition_features": "2.0.0",
    "step_tasks": "2.0.0",
}


class ServiceNotReady(RuntimeError):
    """至少一个在线依赖未满足固定数据契约。"""

    code = "SERVICE_NOT_READY"

    def __init__(self, *, checks: dict[str, dict[str, Any]]) -> None:
        self.checks = checks
        super().__init__(self.code)


def _decode_json_object(raw: object, field_name: str) -> dict[str, Any]:
    if raw is None:
        raise RuntimeError(f"{field_name} is required")
    if isinstance(raw, Mapping):
        decoded = dict(raw)
    else:
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError(f"invalid {field_name}") from exc
        if not isinstance(raw, str):
            raise RuntimeError(f"{field_name} must be a JSON object")
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid {field_name}") from exc
        if not isinstance(decoded, Mapping):
            raise RuntimeError(f"{field_name} must be a JSON object")
        decoded = dict(decoded)
    return decoded


def mysql_fixed_data_probe() -> dict[str, Any]:
    """核对唯一 ready 构建及其 manifest-bound 固定产物。"""
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
        cursor.execute(
            "SELECT build_id, schema_versions, artifact_counts FROM data_builds "
            "WHERE status='ready'"
        )
        ready_rows = cursor.fetchall()
        if len(ready_rows) != 1:
            raise RuntimeError("unique ready build required")
        raw_build_id = ready_rows[0][0]
        if (
            type(raw_build_id) is not str
            or not raw_build_id
            or raw_build_id != raw_build_id.strip()
        ):
            raise RuntimeError("ready build identity required")
        build_id = raw_build_id
        schema_versions = _decode_json_object(ready_rows[0][1], "schema_versions")
        runtime_schema_versions = {
            name: schema_versions.get(name) for name in EXPECTED_RUNTIME_SCHEMA_VERSIONS
        }
        if runtime_schema_versions != EXPECTED_RUNTIME_SCHEMA_VERSIONS:
            raise RuntimeError("runtime artifact schema versions do not match V2 contract")

        expected_counts = _decode_json_object(ready_rows[0][2], "artifact_counts")
        if set(expected_counts) != set(FIXED_ARTIFACT_NAMES):
            raise RuntimeError("artifact_counts keys do not match fixed artifact catalog")
        if any(type(count) is not int or count < 0 for count in expected_counts.values()):
            raise RuntimeError("artifact_counts values must be non-negative integers")

        cursor.execute(
            "SELECT artifact_name, COUNT(*) FROM fixed_artifact_records "
            "WHERE build_id=%s GROUP BY artifact_name",
            (build_id,),
        )
        actual: dict[str, int] = {}
        for raw_name, raw_count in cursor.fetchall():
            if (
                type(raw_name) is not str
                or not raw_name
                or raw_name != raw_name.strip()
                or raw_name not in FIXED_ARTIFACT_NAMES
                or type(raw_count) is not int
                or raw_count < 0
                or raw_name in actual
            ):
                raise RuntimeError("invalid fixed artifact record")
            actual[raw_name] = raw_count
        if actual != expected_counts:
            raise RuntimeError("fixed artifact counts do not match approved manifest")
        recipe_count = expected_counts["recipe_retrieval_build_views"]
        if recipe_count <= 0:
            raise RuntimeError("manifest retrieval count must be positive")
        return {
            "status": "ready",
            "build_id": build_id,
            "artifact_count": len(FIXED_ARTIFACT_NAMES),
            "recipe_count": recipe_count,
            "runtime_schema_versions": runtime_schema_versions,
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
        raw_build_id = mysql["build_id"]
        raw_recipe_count = mysql["recipe_count"]
        raw_artifact_count = mysql["artifact_count"]
        raw_runtime_schema_versions = mysql["runtime_schema_versions"]
        if (
            type(raw_build_id) is not str
            or not raw_build_id
            or raw_build_id != raw_build_id.strip()
            or type(raw_recipe_count) is not int
            or raw_recipe_count <= 0
            or type(raw_artifact_count) is not int
            or raw_artifact_count != len(FIXED_ARTIFACT_NAMES)
            or type(raw_runtime_schema_versions) is not dict
        ):
            raise RuntimeError("invalid fixed data identity")
        runtime_schema_versions = raw_runtime_schema_versions
        if runtime_schema_versions != EXPECTED_RUNTIME_SCHEMA_VERSIONS:
            raise RuntimeError("invalid runtime artifact schema versions")
        build_id = raw_build_id
        recipe_count = raw_recipe_count
        artifact_count = raw_artifact_count
        checks["mysql"] = {
            "status": "ready",
            "artifact_count": artifact_count,
            "recipe_count": recipe_count,
            "runtime_schema_versions": runtime_schema_versions,
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
            point_count = qdrant["point_count"]
            if type(point_count) is not int or point_count != recipe_count:
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
