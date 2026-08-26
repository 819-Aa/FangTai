from __future__ import annotations

from food_agent_v2.core.config import load_config


def test_load_config_uses_standard_default_ports(monkeypatch) -> None:
    for key in (
        "MYSQL_PORT",
        "QDRANT_REST_PORT",
        "QDRANT_GRPC_PORT",
        "REDIS_PORT",
        "API_PORT",
    ):
        monkeypatch.delenv(key, raising=False)

    config = load_config()

    assert config.mysql.port == 3306
    assert config.qdrant.rest_port == 6333
    assert config.qdrant.grpc_port == 6334
    assert config.redis.port == 6379
    assert config.api.port == 8000
