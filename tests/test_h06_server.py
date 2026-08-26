from __future__ import annotations

import os
from pathlib import Path

from food_agent_v2.h06_server import prepare_environment


def test_prepare_environment_loads_main_repo_dotenv_from_worktree(
    tmp_path: Path,
) -> None:
    main_repo = tmp_path / "program_v2"
    worktree = main_repo / ".worktrees" / "h06"
    worktree.mkdir(parents=True)
    (main_repo / ".env").write_text(
        "LLM_API_KEY=test-llm\nSILICONFLOW_API_KEY=test-sf\n",
        encoding="utf-8",
    )
    keys = ("LLM_API_KEY", "SILICONFLOW_API_KEY")
    original = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        prepare_environment(
            worktree,
            mysql_port=3306,
            qdrant_rest_port=6333,
            qdrant_grpc_port=6334,
            redis_port=6379,
            api_port=8000,
        )

        assert os.environ["LLM_API_KEY"] == "test-llm"
        assert os.environ["SILICONFLOW_API_KEY"] == "test-sf"
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_prepare_environment_overrides_historic_service_ports(
    tmp_path: Path, monkeypatch
) -> None:
    with monkeypatch.context() as environment:
        for key, old_value in {
            "MYSQL_PORT": "3309",
            "QDRANT_REST_PORT": "6339",
            "QDRANT_GRPC_PORT": "6340",
            "REDIS_PORT": "6382",
            "API_PORT": "8002",
        }.items():
            environment.setenv(key, old_value)

        prepare_environment(
            tmp_path,
            mysql_port=3306,
            qdrant_rest_port=6333,
            qdrant_grpc_port=6334,
            redis_port=6379,
            api_port=8000,
        )

        assert os.environ["MYSQL_PORT"] == "3306"
        assert os.environ["QDRANT_REST_PORT"] == "6333"
        assert os.environ["QDRANT_GRPC_PORT"] == "6334"
        assert os.environ["REDIS_PORT"] == "6379"
        assert os.environ["API_PORT"] == "8000"
