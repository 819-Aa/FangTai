"""V2 配置加载（环境变量 + 默认值）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """加载项目根目录的 .env 文件。"""
    env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    if not env_path.exists():
        return
    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


@dataclass
class MySQLConfig:
    host: str = "localhost"
    port: int = 3307
    user: str = "foodagent"
    password: str = "foodagent_v2"
    database: str = "food_agent_v2"

    @property
    def dsn(self) -> str:
        return f"mysql+pymysql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@dataclass
class QdrantConfig:
    host: str = "localhost"
    rest_port: int = 6335
    grpc_port: int = 6336
    collection: str = "recipe_retrieval_v2"


@dataclass
class RedisConfig:
    host: str = "localhost"
    port: int = 6380
    key_prefix: str = "v2"


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model_reasoning: str = ""       # query/health/menu/review
    model_answer: str = ""          # answer_generation
    timeout_seconds: int = 30
    max_retries: int = 1
    reasoning_extra_body: dict | None = None
    answer_extra_body: dict | None = None

    def model_for_role(self, role: str) -> str:
        """根据角色返回对应模型。"""
        if role == "answer_generation":
            return self.model_answer or self.model_reasoning
        return self.model_reasoning

    def extra_body_for_role(self, role: str) -> dict | None:
        if role == "answer_generation":
            return self.answer_extra_body
        return self.reasoning_extra_body


@dataclass
class ModelConfig:
    bge_model_path: str = ".model-cache/bge-m3"
    reranker_model_path: str = ".model-cache/bge-reranker-v2-m3"
    rag_warmup_on_startup: bool = True
    low_memory_mode: bool = True
    device: str = "auto"


@dataclass
class APIConfig:
    host: str = "0.0.0.0"
    port: int = 8001
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5174"])


@dataclass
class AppConfig:
    mysql: MySQLConfig = field(default_factory=MySQLConfig)
    qdrant: QdrantConfig = field(default_factory=QdrantConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    api: APIConfig = field(default_factory=APIConfig)


def _parse_json_env(key: str) -> dict | None:
    import json
    val = os.getenv(key, "")
    if val:
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return None
    return None


def load_config() -> AppConfig:
    """从环境变量加载配置。"""
    return AppConfig(
        mysql=MySQLConfig(
            host=os.getenv("MYSQL_HOST", "localhost"),
            port=int(os.getenv("MYSQL_PORT", "3307")),
            user=os.getenv("MYSQL_USER", "foodagent"),
            password=os.getenv("MYSQL_PASSWORD", "foodagent_v2"),
            database=os.getenv("MYSQL_DATABASE", "food_agent_v2"),
        ),
        qdrant=QdrantConfig(
            host=os.getenv("QDRANT_HOST", "localhost"),
            rest_port=int(os.getenv("QDRANT_REST_PORT", "6335")),
            grpc_port=int(os.getenv("QDRANT_GRPC_PORT", "6336")),
            collection=os.getenv("QDRANT_COLLECTION", "recipe_retrieval_v2"),
        ),
        redis=RedisConfig(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6380")),
            key_prefix=os.getenv("REDIS_KEY_PREFIX", "v2"),
        ),
        llm=LLMConfig(
            api_key=os.getenv("LLM_API_KEY", ""),
            base_url=os.getenv("LLM_BASE_URL", ""),
            model_reasoning=os.getenv("LLM_MODEL_REASONING", ""),
            model_answer=os.getenv("LLM_MODEL_ANSWER", ""),
            timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "1")),
            reasoning_extra_body=_parse_json_env("LLM_MODEL_REASONING_EXTRA_BODY"),
            answer_extra_body=_parse_json_env("LLM_MODEL_ANSWER_EXTRA_BODY"),
        ),
        models=ModelConfig(
            bge_model_path=os.getenv("BGE_MODEL_PATH", ".model-cache/bge-m3"),
            reranker_model_path=os.getenv("RERANKER_MODEL_PATH", ".model-cache/bge-reranker-v2-m3"),
            rag_warmup_on_startup=os.getenv("RAG_WARMUP_ON_STARTUP", "true").lower() == "true",
            low_memory_mode=os.getenv("MODEL_LOW_MEMORY_MODE", "true").lower() == "true",
            device=os.getenv("MODEL_DEVICE", "auto").lower(),
        ),
        api=APIConfig(
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8001")),
            cors_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5174").split(",")],
        ),
    )
