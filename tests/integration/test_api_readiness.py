"""MC-04 public liveness/readiness split."""

from fastapi.testclient import TestClient

from food_agent_v2.api_app import app
from food_agent_v2.application import readiness


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
