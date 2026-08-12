"""C1 production models must not overlap on memory-constrained CPU hosts."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

import food_agent_v2.c1 as c1
import food_agent_v2.c1.qdrant_client as qdrant_client
from food_agent_v2.c1 import RecipeRetrievalService, RetrievalCandidate


class _FakeEmbeddingModel:
    def encode(self, text, *, normalize_embeddings):
        assert text == "红烧肉"
        assert normalize_embeddings is True
        return np.asarray([0.1, 0.2], dtype=np.float32)


class _FakeQdrantClient:
    def query_points(self, **kwargs):
        assert kwargs["query"] == pytest.approx([0.1, 0.2], abs=0.001)
        return SimpleNamespace(points=[])


def test_vector_search_releases_owned_embedding_before_qdrant_query(monkeypatch) -> None:
    events: list[str] = []
    model = _FakeEmbeddingModel()

    monkeypatch.setattr(qdrant_client, "_get_embedding_model", lambda: model)
    monkeypatch.setattr(
        qdrant_client,
        "_release_embedding_model",
        lambda: events.append("released"),
    )

    store = qdrant_client.QdrantVectorStore()
    store._client = _FakeQdrantClient()
    monkeypatch.setattr(store, "_connect", lambda: True)

    store.search("红烧肉", top_k=2)

    assert events == ["released"]


def test_production_reranker_is_released_after_prediction(monkeypatch) -> None:
    events: list[str] = []

    class FakeReranker:
        def predict(self, pairs):
            events.append("predicted")
            return [0.8]

    monkeypatch.setattr(c1, "_get_production_reranker", FakeReranker)
    monkeypatch.setattr(c1, "low_memory_model_mode", lambda: True)
    monkeypatch.setattr(c1, "release_model_memory", lambda: events.append("released"))
    service = RecipeRetrievalService(reranker=None)
    candidate = RetrievalCandidate(
        recipe_id=1,
        document_id="recipe_0001",
        name="红烧肉",
        score=1.0,
    )

    result = service._rerank("红烧肉", [candidate], 1)

    assert result[0].rerank_score == 0.8
    assert service._reranker is None
    assert events == ["predicted", "released"]


def test_injected_reranker_is_never_released(monkeypatch) -> None:
    events: list[str] = []

    class InjectedReranker:
        def predict(self, pairs):
            return [0.7]

    reranker = InjectedReranker()
    monkeypatch.setattr(c1, "low_memory_model_mode", lambda: True)
    monkeypatch.setattr(c1, "release_model_memory", lambda: events.append("released"))
    service = RecipeRetrievalService(reranker=reranker)
    candidate = RetrievalCandidate(
        recipe_id=1,
        document_id="recipe_0001",
        name="红烧肉",
        score=1.0,
    )

    service._rerank("红烧肉", [candidate], 1)

    assert service._reranker is reranker
    assert events == []


def test_low_memory_mode_skips_dual_model_warmup(monkeypatch) -> None:
    monkeypatch.setattr(c1, "low_memory_model_mode", lambda: True)
    monkeypatch.setattr(
        qdrant_client,
        "_get_embedding_model",
        lambda: (_ for _ in ()).throw(AssertionError("embedding loaded")),
    )
    monkeypatch.setattr(
        c1,
        "_get_production_reranker",
        lambda: (_ for _ in ()).throw(AssertionError("reranker loaded")),
    )

    c1.warmup_models()
