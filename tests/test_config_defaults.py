from __future__ import annotations

from food_agent_v2.core.config import LLMConfig, load_config


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


def test_query_understanding_uses_its_own_extra_body() -> None:
    config = LLMConfig(
        reasoning_extra_body={"enable_thinking": True},
        query_extra_body={"enable_thinking": False},
        answer_extra_body={"temperature": 0.2},
    )

    assert config.extra_body_for_role("query_understanding") == {"enable_thinking": False}
    assert config.extra_body_for_role("menu_planning") == {"enable_thinking": True}
    assert config.extra_body_for_role("answer_generation") == {"temperature": 0.2}


def test_load_config_parses_query_extra_body(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL_QUERY_EXTRA_BODY", '{"enable_thinking": false}')

    config = load_config()

    assert config.llm.query_extra_body == {"enable_thinking": False}
