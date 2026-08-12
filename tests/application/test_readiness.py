"""MC-04 readiness contract: fixed data, Redis and Qdrant must agree."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from food_agent_v2.application import readiness


BUILD_ID = "8f98393e-4ae2-4c00-bd0b-1cb07cd91a6f"


def _mysql_ready() -> dict:
    return {
        "status": "ready",
        "build_id": BUILD_ID,
        "artifact_count": len(readiness.EXPECTED_FIXED_ARTIFACT_COUNTS),
        "recipe_count": 1914,
    }


def _redis_ready() -> dict:
    return {"status": "ready"}


def _qdrant_ready(build_id: str, expected_count: int) -> dict:
    assert build_id == BUILD_ID
    assert expected_count == 1914
    return {"status": "ready", "point_count": 1914}


def test_readiness_requires_all_three_stores() -> None:
    result = readiness.check_readiness(
        mysql_probe=_mysql_ready,
        redis_probe=_redis_ready,
        qdrant_probe=_qdrant_ready,
    )

    assert result == {
        "status": "ready",
        "build_id": BUILD_ID,
        "checks": {
            "mysql": {"status": "ready", "artifact_count": 19, "recipe_count": 1914},
            "redis": {"status": "ready"},
            "qdrant": {"status": "ready", "point_count": 1914},
        },
    }


@pytest.mark.parametrize("failed_store", ["mysql", "redis", "qdrant"])
def test_readiness_fails_closed_with_public_status_only(failed_store: str) -> None:
    def failed_probe(*_args: object) -> dict:
        raise RuntimeError("private host password or driver details")

    with pytest.raises(readiness.ServiceNotReady) as exc_info:
        readiness.check_readiness(
            mysql_probe=failed_probe if failed_store == "mysql" else _mysql_ready,
            redis_probe=failed_probe if failed_store == "redis" else _redis_ready,
            qdrant_probe=failed_probe if failed_store == "qdrant" else _qdrant_ready,
        )

    exc = exc_info.value
    assert exc.code == "SERVICE_NOT_READY"
    assert exc.checks[failed_store] == {"status": "unavailable"}
    assert "password" not in str(exc.checks)


def test_mysql_probe_requires_exact_fixed_artifact_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = readiness.EXPECTED_FIXED_ARTIFACT_COUNTS
    cursor = SimpleNamespace()
    responses = [
        [(BUILD_ID,)],
        [(name, count) for name, count in expected.items() if name != "user_profiles"],
    ]
    cursor.execute = lambda *_args: None
    cursor.fetchall = lambda: responses.pop(0)
    cursor.close = lambda: None
    connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: None)
    monkeypatch.setattr("pymysql.connect", lambda **_kwargs: connection)

    with pytest.raises(RuntimeError, match="fixed artifact counts"):
        readiness.mysql_fixed_data_probe()


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
