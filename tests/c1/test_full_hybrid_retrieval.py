"""T14 C1 无降级混合检索测试。

词法+向量+RRF+重排全部真实执行；任一不可用明确失败（无降级/无融合回退）；
候选全部 eligible 且属于当前 build；伪 reranker/向量只能经显式注入的 fixture。
"""

import pytest

from food_agent_v2.c1 import RecipeRetrievalService, RetrievalError

BUILD = "build-c1"


class FakeVector:
    def __init__(self, available: bool = True, results: list[tuple[int, float]] | None = None) -> None:
        self._available = available
        self._results = results or [(2, 0.9), (1, 0.8)]

    @property
    def available(self) -> bool:
        return self._available

    def search(self, query: str, top_k: int = 50) -> list[tuple[int, float]]:
        return self._results[:top_k]


class FakeReranker:
    def __init__(self, scores: list[float] | None = None, fail: bool = False) -> None:
        self._scores = scores
        self._fail = fail

    def predict(self, pairs: list[list[str]]) -> list[float]:
        if self._fail:
            raise RuntimeError("reranker down")
        return self._scores or [0.0] * len(pairs)


def _rag(recipe_id: int, name: str, text: str) -> dict:
    return {
        "recipe_id": recipe_id,
        "document_id": f"doc_{recipe_id}",
        "name": name,
        "searchable_text": text,
        "searchable_fields": {"flavor": name},
    }


class FakeSource:
    def ready_build_id(self) -> str:
        return BUILD

    def records(self, artifact_name: str, build_id: str) -> list[dict]:
        if artifact_name == "rag_documents":
            return [_rag(1, "红烧肉", "猪肉 酱油 糖"), _rag(2, "清蒸鱼", "鱼 姜 葱")]
        if artifact_name == "recipe_retrieval_build_views":
            return [
                {"recipe_id": 1, "catalog_eligibility": "eligible"},
                {"recipe_id": 2, "catalog_eligibility": "eligible"},
            ]
        return []


def _service(vector=None, reranker=None, source=None) -> RecipeRetrievalService:
    service = RecipeRetrievalService(
        vector_store=vector or FakeVector(),
        reranker=reranker or FakeReranker([1.0, 0.5]),
        source=source or FakeSource(),
    )
    service.load()
    return service


class TestFullHybridRetrieval:
    def test_full_hybrid_path(self) -> None:
        service = _service()
        result = service.retrieve("红烧肉", top_k=2)
        assert result.retrieval_path == "hybrid_rerank"
        assert len(result.candidates) > 0
        assert all(c.recipe_id in {1, 2} for c in result.candidates)

    def test_vector_unavailable_fails_closed(self) -> None:
        service = _service(vector=FakeVector(available=False))
        with pytest.raises(RetrievalError) as excinfo:
            service.retrieve("红烧肉")
        assert excinfo.value.code == "VECTOR_INDEX_UNAVAILABLE"

    def test_reranker_failure_fails_closed(self) -> None:
        service = _service(reranker=FakeReranker(fail=True))
        with pytest.raises(RetrievalError) as excinfo:
            service.retrieve("红烧肉")
        assert excinfo.value.code == "RERANKER_UNAVAILABLE"

    def test_not_loaded_fails_closed(self) -> None:
        service = RecipeRetrievalService(
            vector_store=FakeVector(), reranker=FakeReranker(), source=FakeSource()
        )
        with pytest.raises(RetrievalError) as excinfo:
            service.retrieve("红烧肉")
        assert excinfo.value.code == "RETRIEVAL_NOT_LOADED"

    def test_ineligible_candidates_filtered(self) -> None:
        source = FakeSource()

        def records(name, build_id):
            if name == "recipe_retrieval_build_views":
                # recipe 2 不可推荐。
                return [{"recipe_id": 1, "catalog_eligibility": "eligible"}]
            return FakeSource.records(source, name, build_id)

        source.records = records
        service = _service(source=source)
        result = service.retrieve("红烧肉", top_k=2)
        assert all(c.recipe_id == 1 for c in result.candidates)

    def test_reranker_ordering_applied(self) -> None:
        # 注入的重排器分数应用到候选（生产用真实 BGE，测试只允许注入 fixture）。
        service = _service(reranker=FakeReranker([0.9, 0.1]))
        result = service.retrieve("红烧肉", top_k=2)
        assert result.candidates[0].rerank_score == 0.9
