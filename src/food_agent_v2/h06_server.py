"""Start the H06 API after loading the owning repository environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _dotenv_paths(repo_root: Path) -> list[Path]:
    paths = [repo_root / ".env"]
    if repo_root.parent.name == ".worktrees":
        paths.append(repo_root.parent.parent / ".env")
    return paths


def _load_dotenv(repo_root: Path) -> None:
    for path in _dotenv_paths(repo_root):
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def prepare_environment(
    repo_root: Path,
    *,
    mysql_port: int,
    qdrant_rest_port: int,
    qdrant_grpc_port: int,
    redis_port: int,
    api_port: int,
) -> None:
    """Load secrets first, then pin H06 to the approved standard ports."""
    _load_dotenv(repo_root.resolve())
    os.environ.update(
        {
            "MYSQL_HOST": "127.0.0.1",
            "MYSQL_PORT": str(mysql_port),
            "QDRANT_HOST": "127.0.0.1",
            "QDRANT_REST_PORT": str(qdrant_rest_port),
            "QDRANT_GRPC_PORT": str(qdrant_grpc_port),
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": str(redis_port),
            "API_HOST": "127.0.0.1",
            "API_PORT": str(api_port),
            "RAG_WARMUP_ON_STARTUP": "true",
        }
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the H06 API with an explicit data-plane binding")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, required=True)
    parser.add_argument("--mysql-port", type=int, required=True)
    parser.add_argument("--qdrant-rest-port", type=int, required=True)
    parser.add_argument("--qdrant-grpc-port", type=int, required=True)
    parser.add_argument("--redis-port", type=int, required=True)
    args = parser.parse_args(argv)

    prepare_environment(
        args.repo_root,
        mysql_port=args.mysql_port,
        qdrant_rest_port=args.qdrant_rest_port,
        qdrant_grpc_port=args.qdrant_grpc_port,
        redis_port=args.redis_port,
        api_port=args.api_port,
    )

    import uvicorn

    uvicorn.run("food_agent_v2.api_app:app", host=args.host, port=args.api_port)


if __name__ == "__main__":
    main()
