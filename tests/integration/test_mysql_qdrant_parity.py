"""T09 MySQL/Qdrant parity and online collection boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import qdrant_client

from food_agent_v2.b1.database_loader import (
    InitializationError,
    initialize_verified_fixed_data,
)
from food_agent_v2.c1.qdrant_client import QdrantVectorStore
from food_agent_v2.core.config import QdrantConfig

from .test_staging_initialization import FakeMySQLTarget, FakeVectorTarget


def test_qdrant_missing_one_point_blocks_commit(verified_build: Path) -> None:
    mysql = FakeMySQLTarget(retain_artifacts={"rag_documents"})
    vector = FakeVectorTarget(drop_last_point=True)

    with pytest.raises(InitializationError, match="VECTOR_INDEX_PARITY_FAILED"):
        initialize_verified_fixed_data(
            verified_build,
            confirm_empty_v2=True,
            mysql_target=mysql,
            vector_target=vector,
            final_collection="recipe_retrieval_v2",
        )

    assert mysql.rolled_back is True
    assert mysql.committed == {}
    assert vector.collections == {}
    assert vector.published == {}


def test_successful_initialization_has_mysql_qdrant_recipe_parity(verified_build: Path) -> None:
    mysql = FakeMySQLTarget(retain_artifacts={"rag_documents"})
    vector = FakeVectorTarget()

    report = initialize_verified_fixed_data(
        verified_build,
        confirm_empty_v2=True,
        mysql_target=mysql,
        vector_target=vector,
        final_collection="recipe_retrieval_v2",
    )

    rag_ids = {int(item["recipe_id"]) for item in mysql.committed["rag_documents"]}
    physical = vector.published["recipe_retrieval_v2"]
    assert report["status"] == "initialized"
    assert len(report["manifest_sha256"]) == 64
    evidence_path = Path(report["initialization_report_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "initialized"
    assert evidence["qdrant_point_count"] == 1914
    assert report["qdrant_point_count"] == 1914
    assert vector.point_ids(physical) == rag_ids


def test_online_qdrant_config_exposes_one_collection_name() -> None:
    config = QdrantConfig()
    assert config.collection == "recipe_retrieval_v2"
    assert not hasattr(config, "staging_collection")
    assert not hasattr(config, "collection_version")


def test_online_vector_store_recognizes_published_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    class AliasOnlyClient:
        def __init__(self, **_: object) -> None:
            self.create_calls = 0

        def get_collections(self) -> SimpleNamespace:
            return SimpleNamespace(collections=[])

        def get_aliases(self) -> SimpleNamespace:
            alias = SimpleNamespace(
                alias_name="recipe_retrieval_v2",
                collection_name="recipe_retrieval_v2__staging__fixed",
            )
            return SimpleNamespace(aliases=[alias])

        def create_collection(self, **_: object) -> None:
            self.create_calls += 1
            raise AssertionError("published alias must not be replaced by a collection")

    client = AliasOnlyClient()
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **_: client)

    store = QdrantVectorStore()

    assert store.available is True
    assert client.create_calls == 0


def test_online_vector_store_does_not_create_unverified_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingTargetClient:
        def __init__(self, **_: object) -> None:
            self.create_calls = 0

        def get_collections(self) -> SimpleNamespace:
            return SimpleNamespace(collections=[])

        def get_aliases(self) -> SimpleNamespace:
            return SimpleNamespace(aliases=[])

        def create_collection(self, **_: object) -> None:
            self.create_calls += 1

    client = MissingTargetClient()
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **_: client)

    store = QdrantVectorStore()

    assert store.available is False
    assert client.create_calls == 0
