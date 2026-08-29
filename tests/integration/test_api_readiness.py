"""MC-04 public liveness/readiness split."""

from fastapi.testclient import TestClient

from food_agent_v2.api_app import app
from food_agent_v2.application import readiness
from food_agent_v2.contracts.build import FIXED_ARTIFACT_NAMES


def test_readiness_rejects_old_runtime_artifact_versions() -> None:
    def mysql_probe():
        return {
            "status": "ready",
            "build_id": "build-old",
            "artifact_count": len(FIXED_ARTIFACT_NAMES),
            "recipe_count": 1932,
            "runtime_schema_versions": {
                "rag_documents": "1.0.0",
                "nutrition_features": "1.0.0",
                "step_tasks": "1.0.0",
            },
        }

    qdrant_calls: list[tuple[str, int]] = []

    def qdrant_probe(build_id: str, count: int):
        qdrant_calls.append((build_id, count))
        return {"status": "ready", "point_count": count}

    try:
        readiness.check_readiness(
            mysql_probe=mysql_probe,
            redis_probe=lambda: {"status": "ready"},
            qdrant_probe=qdrant_probe,
            siliconflow_probe=lambda: {"status": "ready"},
        )
    except readiness.ServiceNotReady as exc:
        assert exc.checks["mysql"]["status"] == "unavailable"
        assert qdrant_calls == []
    else:
        raise AssertionError("旧运行时 Artifact 版本必须阻止 readiness")


def test_health_remains_io_free_when_readiness_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness,
        "check_readiness",
        lambda: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_returns_machine_readable_success(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness,
        "check_readiness",
        lambda: {
            "status": "ready",
            "build_id": "build-1",
            "checks": {
                "mysql": {"status": "ready"},
                "redis": {"status": "ready"},
                "qdrant": {"status": "ready"},
            },
        },
    )

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json()["build_id"] == "build-1"


def test_ready_returns_stable_503_without_internal_details(monkeypatch) -> None:
    error = readiness.ServiceNotReady(
        checks={
            "mysql": {"status": "ready"},
            "redis": {"status": "unavailable"},
            "qdrant": {"status": "ready"},
        }
    )
    monkeypatch.setattr(
        readiness,
        "check_readiness",
        lambda: (_ for _ in ()).throw(error),
    )

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "error": "SERVICE_NOT_READY",
        "message": "service not ready",
        "checks": error.checks,
    }
