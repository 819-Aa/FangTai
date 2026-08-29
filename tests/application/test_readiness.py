"""MC-04 readiness contract: fixed data, Redis and Qdrant must agree."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from food_agent_v2.application import readiness
from food_agent_v2.contracts.build import FIXED_ARTIFACT_NAMES

BUILD_ID = "8f98393e-4ae2-4c00-bd0b-1cb07cd91a6f"
MANIFEST_COUNTS = {
    "recipe_source_rows": 2000,
    "recipe_classifications": 2000,
    "user_profiles": 50,
    "ingredient_occurrences": 17509,
    "ingredient_registry": 1771,
    "ingredient_aliases": 21,
    "ingredient_forms": 381,
    "ingredient_crosswalk": 431,
    "recipe_ingredient_relations": 17493,
    "recipe_health_views": 1932,
    "recipe_step_binding_views": 1932,
    "recipe_nutrition_input_views": 1932,
    "recipe_retrieval_build_views": 1941,
    "step_tasks": 1932,
    "nutrition_features": 1932,
    "rag_documents": 1932,
    "health_relation_decisions": 65626,
    "health_relations": 985,
    "health_relation_coverage": 38,
    "recipe_dependencies": 167,
}
LEGACY_COUNTS = {
    "health_relation_coverage": 38,
    "health_relation_decisions": 65626,
    "health_relations": 985,
    "ingredient_aliases": 21,
    "ingredient_crosswalk": 431,
    "ingredient_forms": 381,
    "ingredient_occurrences": 17509,
    "ingredient_registry": 1771,
    "nutrition_features": 1932,
    "rag_documents": 1932,
    "recipe_classifications": 2000,
    "recipe_health_views": 1932,
    "recipe_ingredient_relations": 17493,
    "recipe_nutrition_input_views": 1932,
    "recipe_retrieval_build_views": 1932,
    "recipe_source_rows": 2000,
    "recipe_step_binding_views": 1932,
    "step_tasks": 1932,
    "user_profiles": 50,
}


def test_h05_fixed_artifact_counts_are_not_owned_by_readiness() -> None:
    assert not hasattr(readiness, "EXPECTED_FIXED_ARTIFACT_COUNTS")
    assert len(FIXED_ARTIFACT_NAMES) == 20


def _mysql_ready() -> dict:
    return {
        "status": "ready",
        "build_id": BUILD_ID,
        "artifact_count": len(FIXED_ARTIFACT_NAMES),
        "recipe_count": 1941,
        "runtime_schema_versions": dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS),
    }


def _redis_ready() -> dict:
    return {"status": "ready"}


def _qdrant_ready(build_id: str, expected_count: int) -> dict:
    assert build_id == BUILD_ID
    assert expected_count == 1941
    return {"status": "ready", "point_count": 1941}


def _siliconflow_ready() -> dict:
    return {
        "status": "ready",
        "models": {
            "embedding": "BAAI/bge-m3",
            "rerank": "BAAI/bge-reranker-v2-m3",
        },
    }


def test_readiness_requires_all_four_stores() -> None:
    result = readiness.check_readiness(
        mysql_probe=_mysql_ready,
        redis_probe=_redis_ready,
        qdrant_probe=_qdrant_ready,
        siliconflow_probe=_siliconflow_ready,
    )

    assert result == {
        "status": "ready",
        "build_id": BUILD_ID,
        "checks": {
            "mysql": {
                "status": "ready",
                "artifact_count": 20,
                "recipe_count": 1941,
                "runtime_schema_versions": dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS),
            },
            "redis": {"status": "ready"},
            "qdrant": {"status": "ready", "point_count": 1941},
            "siliconflow": {
                "status": "ready",
                "models": {
                    "embedding": "BAAI/bge-m3",
                    "rerank": "BAAI/bge-reranker-v2-m3",
                },
            },
        },
    }


def test_readiness_forwards_manifest_recipe_count_to_qdrant() -> None:
    seen: list[tuple[str, int]] = []

    def qdrant(build_id: str, expected_count: int) -> dict:
        seen.append((build_id, expected_count))
        return {"status": "ready", "point_count": expected_count}

    result = readiness.check_readiness(
        mysql_probe=lambda: {
            "status": "ready",
            "build_id": BUILD_ID,
            "artifact_count": 20,
            "recipe_count": 2007,
            "runtime_schema_versions": dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS),
        },
        redis_probe=_redis_ready,
        qdrant_probe=qdrant,
        siliconflow_probe=_siliconflow_ready,
    )

    assert result["status"] == "ready"
    assert seen == [(BUILD_ID, 2007)]


def test_readiness_rejects_empty_build_identity_from_probe() -> None:
    with pytest.raises(readiness.ServiceNotReady) as exc_info:
        readiness.check_readiness(
            mysql_probe=lambda: {
                "status": "ready",
                "build_id": None,
                "artifact_count": 20,
                "recipe_count": 2007,
                "runtime_schema_versions": dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS),
            },
            redis_probe=_redis_ready,
            qdrant_probe=lambda *_args: {"status": "ready", "point_count": 2007},
            siliconflow_probe=_siliconflow_ready,
        )

    assert exc_info.value.checks["mysql"] == {"status": "unavailable"}


def test_readiness_rejects_blank_build_identity_from_probe() -> None:
    with pytest.raises(readiness.ServiceNotReady):
        readiness.check_readiness(
            mysql_probe=lambda: {
                "status": "ready",
                "build_id": "   ",
                "artifact_count": 20,
                "recipe_count": 2007,
                "runtime_schema_versions": dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS),
            },
            redis_probe=_redis_ready,
            qdrant_probe=lambda *_args: {"status": "ready", "point_count": 2007},
            siliconflow_probe=_siliconflow_ready,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_count", 19),
        (
            "runtime_schema_versions",
            {"rag_documents": "1.0.0", "nutrition_features": "1.0.0", "step_tasks": "1.0.0"},
        ),
        ("build_id", ""),
        ("build_id", "   "),
        ("build_id", True),
        ("build_id", 123),
        ("build_id", None),
    ],
)
def test_readiness_rejects_invalid_mysql_result_without_qdrant_call(
    field: str, value: object
) -> None:
    mysql = {
        "status": "ready",
        "build_id": BUILD_ID,
        "artifact_count": 20,
        "recipe_count": 2007,
        "runtime_schema_versions": dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS),
    }
    mysql[field] = value
    qdrant_calls: list[tuple[str, int]] = []

    def qdrant(build_id: str, expected_count: int) -> dict:
        qdrant_calls.append((build_id, expected_count))
        return {"status": "ready", "point_count": expected_count}

    with pytest.raises(readiness.ServiceNotReady) as exc_info:
        readiness.check_readiness(
            mysql_probe=lambda: mysql,
            redis_probe=_redis_ready,
            qdrant_probe=qdrant,
            siliconflow_probe=_siliconflow_ready,
        )

    assert exc_info.value.checks["mysql"] == {"status": "unavailable"}
    assert qdrant_calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("build_id", True),
        ("build_id", 123),
        ("build_id", None),
        ("build_id", " "),
        ("recipe_count", True),
        ("recipe_count", "2007"),
        ("recipe_count", 2007.0),
        ("artifact_count", True),
        ("artifact_count", "20"),
        ("artifact_count", 20.0),
    ],
)
def test_readiness_rejects_non_exact_mysql_result_types_without_qdrant_call(
    field: str, value: object
) -> None:
    mysql = {
        "status": "ready",
        "build_id": BUILD_ID,
        "artifact_count": 20,
        "recipe_count": 2007,
        "runtime_schema_versions": dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS),
    }
    mysql[field] = value
    qdrant_calls: list[tuple[str, int]] = []

    with pytest.raises(readiness.ServiceNotReady):
        readiness.check_readiness(
            mysql_probe=lambda: mysql,
            redis_probe=_redis_ready,
            qdrant_probe=lambda build_id, count: qdrant_calls.append((build_id, count))
            or {"status": "ready", "point_count": count},
            siliconflow_probe=_siliconflow_ready,
        )

    assert qdrant_calls == []


@pytest.mark.parametrize("build_id", [True, 123, " build-1 ", "   "])
def test_mysql_probe_rejects_noncanonical_raw_build_id(
    monkeypatch: pytest.MonkeyPatch, build_id: object
) -> None:
    with pytest.raises(RuntimeError, match="ready build identity"):
        _run_mysql_probe(monkeypatch, MANIFEST_COUNTS, MANIFEST_COUNTS, build_id=build_id)


def test_readiness_rejects_noncanonical_build_id_without_qdrant_call() -> None:
    qdrant_calls: list[tuple[str, int]] = []

    with pytest.raises(readiness.ServiceNotReady):
        readiness.check_readiness(
            mysql_probe=lambda: {
                "status": "ready",
                "build_id": " build-1 ",
                "artifact_count": 20,
                "recipe_count": 2007,
                "runtime_schema_versions": dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS),
            },
            redis_probe=_redis_ready,
            qdrant_probe=lambda build_id, count: qdrant_calls.append((build_id, count))
            or {"status": "ready", "point_count": count},
            siliconflow_probe=_siliconflow_ready,
        )

    assert qdrant_calls == []


@pytest.mark.parametrize("point_count", [True, 2007.0, "2007", "   ", 2007.9])
def test_readiness_rejects_non_exact_qdrant_point_count(point_count: object) -> None:
    mysql = _mysql_ready()
    mysql["recipe_count"] = 2007

    with pytest.raises(readiness.ServiceNotReady) as exc_info:
        readiness.check_readiness(
            mysql_probe=lambda: mysql,
            redis_probe=_redis_ready,
            qdrant_probe=lambda _build_id, _count: {
                "status": "ready",
                "point_count": point_count,
            },
            siliconflow_probe=_siliconflow_ready,
        )

    assert exc_info.value.checks["qdrant"] == {"status": "unavailable"}


@pytest.mark.parametrize("failed_store", ["mysql", "redis", "qdrant", "siliconflow"])
def test_readiness_fails_closed_with_public_status_only(failed_store: str) -> None:
    def failed_probe(*_args: object) -> dict:
        raise RuntimeError("private host password or driver details")

    with pytest.raises(readiness.ServiceNotReady) as exc_info:
        readiness.check_readiness(
            mysql_probe=failed_probe if failed_store == "mysql" else _mysql_ready,
            redis_probe=failed_probe if failed_store == "redis" else _redis_ready,
            qdrant_probe=failed_probe if failed_store == "qdrant" else _qdrant_ready,
            siliconflow_probe=failed_probe if failed_store == "siliconflow" else _siliconflow_ready,
        )

    exc = exc_info.value
    assert exc.code == "SERVICE_NOT_READY"
    assert exc.checks[failed_store] == {"status": "unavailable"}
    assert "password" not in str(exc.checks)


class RecordingCursor:
    def __init__(self, ready_row: tuple, actual_rows: list[tuple]) -> None:
        self.ready_row = ready_row
        self.actual_rows = actual_rows
        self.queries: list[tuple[str, tuple]] = []
        self.closed = False

    def execute(self, query: str, params: tuple = ()) -> None:
        self.queries.append((query, params))

    def fetchall(self) -> list[tuple]:
        return [self.ready_row] if len(self.queries) == 1 else self.actual_rows

    def close(self) -> None:
        self.closed = True


def _run_mysql_probe(
    monkeypatch: pytest.MonkeyPatch,
    manifest: object,
    actual: dict[str, int],
    build_id: object = BUILD_ID,
    actual_rows: list[tuple[object, object]] | None = None,
) -> RecordingCursor:
    cursor = RecordingCursor(
        (build_id, dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS), manifest),
        list(actual.items()) if actual_rows is None else actual_rows,
    )
    connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: None)
    monkeypatch.setattr("pymysql.connect", lambda **_kwargs: connection)
    readiness.mysql_fixed_data_probe()
    return cursor


@pytest.mark.parametrize(
    "manifest",
    [MANIFEST_COUNTS, json.dumps(MANIFEST_COUNTS), json.dumps(MANIFEST_COUNTS).encode()],
)
def test_mysql_probe_accepts_manifest_json_object_encodings(
    monkeypatch: pytest.MonkeyPatch, manifest: object
) -> None:
    cursor = _run_mysql_probe(monkeypatch, manifest, MANIFEST_COUNTS)

    assert cursor.queries[0][0].strip() == (
        "SELECT build_id, schema_versions, artifact_counts FROM data_builds WHERE status='ready'"
    )


def test_mysql_probe_returns_manifest_bound_counts_when_actual_rows_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _run_mysql_probe(monkeypatch, MANIFEST_COUNTS, MANIFEST_COUNTS)

    assert cursor.queries[1][0].startswith("SELECT artifact_name, COUNT(*)")


def test_mysql_probe_rejects_empty_ready_build_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="ready build identity"):
        _run_mysql_probe(monkeypatch, MANIFEST_COUNTS, MANIFEST_COUNTS, build_id=None)


def test_mysql_probe_rejects_actual_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = dict(MANIFEST_COUNTS)
    actual["recipe_dependencies"] -= 1

    with pytest.raises(RuntimeError, match="fixed artifact counts"):
        _run_mysql_probe(monkeypatch, MANIFEST_COUNTS, actual)


@pytest.mark.parametrize(
    "bad_row",
    [
        ("recipe_dependencies", "167"),
        ("recipe_dependencies", True),
        ("recipe_dependencies", 167.5),
        ("recipe_dependencies", -1),
        (123, 167),
        (" recipe_dependencies", 167),
        ("", 167),
    ],
)
def test_mysql_probe_rejects_invalid_actual_rows(
    monkeypatch: pytest.MonkeyPatch, bad_row: tuple[object, object]
) -> None:
    actual_rows = list(MANIFEST_COUNTS.items())
    actual_rows[actual_rows.index(("recipe_dependencies", 167))] = bad_row

    with pytest.raises(RuntimeError, match="fixed artifact record"):
        _run_mysql_probe(
            monkeypatch,
            MANIFEST_COUNTS,
            MANIFEST_COUNTS,
            actual_rows=actual_rows,
        )


def test_mysql_probe_rejects_duplicate_actual_artifact_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_rows = [*MANIFEST_COUNTS.items(), ("recipe_dependencies", 167)]

    with pytest.raises(RuntimeError, match="fixed artifact record"):
        _run_mysql_probe(
            monkeypatch,
            MANIFEST_COUNTS,
            MANIFEST_COUNTS,
            actual_rows=actual_rows,
        )


def test_readiness_invalid_actual_row_does_not_call_qdrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_rows = list(MANIFEST_COUNTS.items())
    actual_rows[actual_rows.index(("recipe_dependencies", 167))] = (
        "recipe_dependencies",
        "167",
    )
    cursor = RecordingCursor(
        (BUILD_ID, dict(readiness.EXPECTED_RUNTIME_SCHEMA_VERSIONS), MANIFEST_COUNTS),
        actual_rows,
    )
    connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: None)
    monkeypatch.setattr("pymysql.connect", lambda **_kwargs: connection)
    qdrant_calls: list[tuple[str, int]] = []

    with pytest.raises(readiness.ServiceNotReady):
        readiness.check_readiness(
            mysql_probe=readiness.mysql_fixed_data_probe,
            redis_probe=_redis_ready,
            qdrant_probe=lambda build_id, count: qdrant_calls.append((build_id, count))
            or {"status": "ready", "point_count": count},
            siliconflow_probe=_siliconflow_ready,
        )

    assert qdrant_calls == []


def test_mysql_probe_rejects_manifest_missing_recipe_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = {name: count for name, count in MANIFEST_COUNTS.items() if name != "recipe_dependencies"}

    with pytest.raises(RuntimeError, match="artifact_counts"):
        _run_mysql_probe(monkeypatch, manifest, LEGACY_COUNTS)


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        "not-json",
        "[]",
        '"scalar"',
        {**MANIFEST_COUNTS, "rag_documents": "1932"},
        {**MANIFEST_COUNTS, "rag_documents": -1},
        {**MANIFEST_COUNTS, "rag_documents": True},
    ],
)
def test_mysql_probe_rejects_invalid_manifest_counts(
    monkeypatch: pytest.MonkeyPatch, manifest: object
) -> None:
    with pytest.raises(RuntimeError, match="artifact_counts"):
        _run_mysql_probe(monkeypatch, manifest, LEGACY_COUNTS)


def test_mysql_probe_closes_resources_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = {"cursor": False, "connection": False}
    cursor = SimpleNamespace()
    cursor.execute = lambda *_args: (_ for _ in ()).throw(RuntimeError("db failed"))
    cursor.close = lambda: closed.__setitem__("cursor", True)
    connection = SimpleNamespace(
        cursor=lambda: cursor,
        close=lambda: closed.__setitem__("connection", True),
    )
    monkeypatch.setattr("pymysql.connect", lambda **_kwargs: connection)

    with pytest.raises(RuntimeError, match="db failed"):
        readiness.mysql_fixed_data_probe()

    assert closed == {"cursor": True, "connection": True}
