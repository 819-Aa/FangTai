"""C1 混合 RAG 检索（T14）。

词法(BM25) + 向量(BGE-M3→Qdrant) + RRF + 重排(BGE-Reranker) 全部真实执行；
任一不可用即整体失败（无降级、无融合回退）。候选必须属于当前 ready 构建且
catalog_eligibility=eligible。数据来源为固定 rag_documents/step_tasks 等
Repository（无 JSONL）。内存/伪 reranker 只允许通过显式注入的 fixture。
"""

from __future__ import annotations

import gc
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from food_agent_v2.b3.repository import MySQLArtifactRecordSource
from food_agent_v2.c1.qdrant_client import QdrantVectorStore
from food_agent_v2.core.config import load_config

RRF_K = 60
LEXICAL_WEIGHT = 0.3
VECTOR_WEIGHT = 0.7


class RetrievalError(RuntimeError):
    """C1 检索确定性失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class RerankerPort(Protocol):
    def predict(self, pairs: list[list[str]]) -> list[float]: ...


class ArtifactSource(Protocol):
    def ready_build_id(self) -> str: ...
    def records(self, artifact_name: str, build_id: str) -> list[dict]: ...


@dataclass
class RetrievalCandidate:
    recipe_id: int
    document_id: str
    name: str
    score: float
    lexical_score: float = 0.0
    vector_score: float = 0.0
    rerank_score: float | None = None
    searchable_fields: dict[str, str] = field(default_factory=dict)
    source_paths: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    retrieval_id: str
    request_id: str | None
    candidates: list[RetrievalCandidate]
    total_candidates: int
    retrieval_path: str
    is_expansion: bool = False
    expansion_source_id: str | None = None
    source_paths: list[str] = field(default_factory=list)


class BM25Index:
    """简化 BM25 索引（词法检索）。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: dict[int, dict] = {}
        self._doc_lengths: dict[int, int] = {}
        self._avg_dl: float = 0
        self._idf: dict[str, float] = {}
        self._term_freqs: dict[str, dict[int, int]] = defaultdict(dict)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        text = text.lower().strip()
        tokens = []
        for i in range(len(text) - 1):
            tokens.append(text[i:i + 2])
        tokens.extend(list(text))
        return tokens

    def index(self, docs: list[dict]) -> None:
        self._docs.clear()
        self._term_freqs.clear()
        for doc in docs:
            rid = doc["recipe_id"]
            self._docs[rid] = doc
            text = doc.get("searchable_text", doc.get("name", ""))
            tokens = self._tokenize(text)
            self._doc_lengths[rid] = len(tokens)
            term_counts: dict[str, int] = defaultdict(int)
            for token in tokens:
                term_counts[token] += 1
            for term, count in term_counts.items():
                self._term_freqs[term][rid] = count
        n_docs = len(self._docs)
        if n_docs == 0:
            return
        self._avg_dl = sum(self._doc_lengths.values()) / n_docs
        for term, doc_freqs in self._term_freqs.items():
            df = len(doc_freqs)
            self._idf[term] = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)

    def search(self, query: str, top_k: int = 50) -> list[tuple[int, float]]:
        tokens = self._tokenize(query)
        scores: dict[int, float] = defaultdict(float)
        for term in tokens:
            idf = self._idf.get(term, 0)
            if idf == 0:
                continue
            for rid, tf in self._term_freqs.get(term, {}).items():
                dl = self._doc_lengths.get(rid, 1)
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * dl / self._avg_dl)
                scores[rid] += idf * numerator / denominator
        return sorted(scores.items(), key=lambda x: -x[1])[:top_k]

    def __len__(self) -> int:
        return len(self._docs)


def _get_production_reranker() -> RerankerPort:
    # 默认硅基流动 API 重排；本地 CrossEncoder 加载已注释备用
    from food_agent_v2.c1.siliconflow import SiliconFlowReranker

    return SiliconFlowReranker()
    # ---- 本地 CrossEncoder 加载（CPU 冷加载慢，已停用备用）----
    # from sentence_transformers import CrossEncoder
    # from food_agent_v2.c1.qdrant_client import _model_device, _model_source
    # cfg = load_config().models
    # return CrossEncoder(
    #     _model_source(cfg.reranker_model_path, "BAAI/bge-reranker-v2-m3"),
    #     cache_folder=".model-cache",
    #     device=_model_device(),
    # )


def low_memory_model_mode() -> bool:
    return load_config().models.low_memory_mode


def release_model_memory() -> None:
    gc.collect()


class RecipeRetrievalService:
    """菜品混合检索服务（无降级；任一链路不可用即失败）。"""

    def __init__(
        self,
        *,
        vector_store: QdrantVectorStore | None = None,
        reranker: RerankerPort | None = None,
        source: ArtifactSource | None = None,
    ) -> None:
        self._index = BM25Index()
        self._vector = vector_store or QdrantVectorStore()
        self._reranker = reranker  # 生产默认惰性加载真实 BGE；测试注入 fixture
        self._owns_reranker = reranker is None
        self._source = source or MySQLArtifactRecordSource()
        self._build_id: str | None = None
        self._eligible: set[int] = set()
        self._time_lookup: dict[int, dict] = {}
        self._loaded = False

    def load(self, path=None) -> None:
        """从固定 Artifact 构建内存索引（path 兼容旧签名）。"""
        build_id = self._source.ready_build_id()
        self._build_id = build_id
        rag_docs = self._source.records("rag_documents", build_id)
        self._index.index(rag_docs)

        views = self._source.records("recipe_retrieval_build_views", build_id)
        self._eligible = {
            int(v["recipe_id"])
            for v in views
            if v.get("catalog_eligibility") == "eligible"
        }

        step_rows = self._source.records("step_tasks", build_id)
        self._time_lookup = {}
        for rec in step_rows:
            total_s = rec.get("total_duration_seconds")
            if rec.get("authority") == "deterministic_high" and total_s:
                self._time_lookup[int(rec["recipe_id"])] = {
                    "total_minutes": int(total_s) // 60,
                    "confidence": "high",
                }
        self._loaded = True

    @property
    def build_id(self) -> str | None:
        return self._build_id

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            raise RetrievalError("RETRIEVAL_NOT_LOADED", "检索服务未加载")

    def retrieve(
        self, query: str, top_k: int = 20, exclude_ids: list[int] | None = None
    ) -> RetrievalResult:
        """完整混合检索；词法/向量/重排任一不可用即失败。"""
        self._ensure_loaded()

        if not self._vector.available:
            raise RetrievalError("VECTOR_INDEX_UNAVAILABLE", "Qdrant 向量索引不可用，不降级")

        exclude = set(exclude_ids or [])
        lexical = [(rid, s) for rid, s in self._index.search(query, top_k=100) if rid not in exclude]
        vector = [(rid, s) for rid, s in self._vector.search(query, top_k=100) if rid not in exclude]

        fused = self._rrf_fuse(lexical, vector)
        candidates = self._normalize_and_build(fused, max(top_k, 30))
        candidates = self._rerank(query, candidates, top_k)
        candidates = self._apply_time_boost(query, candidates, top_k)

        # 只保留当前构建 eligible 候选。
        candidates = [c for c in candidates if c.recipe_id in self._eligible]

        return RetrievalResult(
            retrieval_id=f"retr_{len(candidates)}",
            request_id=None,
            candidates=candidates,
            total_candidates=len(candidates),
            retrieval_path="hybrid_rerank",
            source_paths=["hybrid_rerank"],
        )

    def _rrf_fuse(
        self, lexical: list[tuple[int, float]], vector: list[tuple[int, float]]
    ) -> list[tuple[int, float, float, float]]:
        scores: dict[int, dict[str, float]] = defaultdict(lambda: {"lex": 0.0, "vec": 0.0})
        for rank, (rid, _) in enumerate(lexical):
            scores[rid]["lex"] = LEXICAL_WEIGHT / (RRF_K + rank + 1)
        for rank, (rid, _) in enumerate(vector):
            scores[rid]["vec"] = VECTOR_WEIGHT / (RRF_K + rank + 1)
        fused = [
            (rid, s["lex"] + s["vec"], s["lex"], s["vec"])
            for rid, s in scores.items()
        ]
        fused.sort(key=lambda x: -x[1])
        return fused

    def _normalize_and_build(
        self, ranked: list[tuple[int, float, float, float]], top_k: int
    ) -> list[RetrievalCandidate]:
        if not ranked:
            return []
        max_s = max(r[1] for r in ranked)
        min_s = min(r[1] for r in ranked)
        score_range = max_s - min_s or 1
        candidates = []
        for i, (rid, fused, lex_s, vec_s) in enumerate(ranked[:top_k]):
            doc = self._index._docs.get(rid, {})
            fields = doc.get("searchable_fields", {})
            candidates.append(
                RetrievalCandidate(
                    recipe_id=rid,
                    document_id=doc.get("document_id", f"recipe_{rid:04d}"),
                    name=doc.get("name", ""),
                    score=round((fused - min_s) / score_range, 4),
                    lexical_score=round(lex_s, 4),
                    vector_score=round(vec_s, 4),
                    searchable_fields={k: v for k, v in fields.items()},
                    source_paths=[f"rrf:rank:{i + 1}"],
                )
            )
        return candidates

    def _candidate_rerank_text(self, candidate: RetrievalCandidate) -> str:
        extra = " ".join(str(v) for v in (candidate.searchable_fields or {}).values() if v)
        return f"{candidate.name} {extra}".strip()

    def _rerank(
        self, query: str, candidates: list[RetrievalCandidate], top_k: int
    ) -> list[RetrievalCandidate]:
        """重排必须真实执行；无重排器或推理失败即整体失败（不融合回退）。"""
        if not candidates:
            return candidates
        if self._reranker is None:
            self._reranker = _get_production_reranker()
        pairs = [[query, self._candidate_rerank_text(c)] for c in candidates]
        try:
            scores = list(self._reranker.predict(pairs))
        except Exception as exc:
            raise RetrievalError("RERANKER_UNAVAILABLE", f"重排失败: {exc}") from exc
        finally:
            if self._owns_reranker and low_memory_model_mode():
                self._reranker = None
                release_model_memory()
        if len(scores) != len(candidates):
            raise RetrievalError("RERANKER_UNAVAILABLE", "重排结果数量与候选不一致")
        ranked = sorted(zip(candidates, scores, strict=True), key=lambda x: -x[1])
        result = []
        for c, s in ranked[:top_k]:
            c.rerank_score = float(s)
            result.append(c)
        return result

    def _apply_time_boost(
        self, query: str, candidates: list[RetrievalCandidate], top_k: int
    ) -> list[RetrievalCandidate]:
        time_kws = ("快手", "半小时", "30分钟", "快速", "快一点", "时间短", "省时", "时间紧", "尽快")
        if not any(kw in query for kw in time_kws):
            return candidates
        for c in candidates:
            info = self._time_lookup.get(c.recipe_id)
            if info and info.get("confidence") == "high" and (info.get("total_minutes") or 999) <= 30:
                c.rerank_score = (c.rerank_score if c.rerank_score is not None else c.score) * 1.15
        candidates.sort(
            key=lambda x: -(x.rerank_score if x.rerank_score is not None else x.score)
        )
        return candidates[:top_k]

    def multi_person_retrieve(
        self, shared_query: str, per_participant_prefs: list[list[str]], top_k: int = 50
    ) -> RetrievalResult:
        merged: dict[int, RetrievalCandidate] = {}
        shared = self.retrieve(shared_query, top_k=min(top_k, 30))
        for c in shared.candidates:
            c.source_paths = ["shared"]
            merged[c.recipe_id] = c
        # 并行执行每参与者的子查询（L3：多人查询并行，缩短多路检索延迟）。
        # retrieve 内部只读 BM25/Qdrant + 线程安全 SiliconFlow httpx 连接池，可并发。
        tasks = [(i, prefs) for i, prefs in enumerate(per_participant_prefs) if prefs]
        if tasks:
            from concurrent.futures import ThreadPoolExecutor

            def _sub(task):
                i, prefs = task
                sub_query = f"{shared_query} {' '.join(prefs[:3])}".strip()
                return i, self.retrieve(sub_query, top_k=min(top_k, 20))

            with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as pool:
                sub_results = pool.map(_sub, tasks)
            for i, sub in sub_results:
                for c in sub.candidates:
                    if c.recipe_id in merged:
                        if f"participant_{i + 1}" not in merged[c.recipe_id].source_paths:
                            merged[c.recipe_id].source_paths.append(f"participant_{i + 1}")
                    else:
                        c.source_paths = [f"participant_{i + 1}"]
                        merged[c.recipe_id] = c
        cands = sorted(merged.values(), key=lambda c: -c.score)[:top_k]
        return RetrievalResult(
            retrieval_id=f"multi_{len(cands)}",
            request_id=None,
            candidates=cands,
            total_candidates=len(cands),
            retrieval_path="multi_person_hybrid_rerank",
            source_paths=["shared"] + [f"participant_{i + 1}" for i in range(len(per_participant_prefs))],
        )

    def expand_retrieval(self, original_result: RetrievalResult, query_plan: dict) -> RetrievalResult | None:
        self._ensure_loaded()
        original_ids = {c.recipe_id for c in original_result.candidates}
        relaxed_query = query_plan.get("relaxed_query", query_plan.get("query", ""))
        result = self.retrieve(relaxed_query, top_k=30, exclude_ids=list(original_ids))
        if result.total_candidates == 0:
            return None
        result.is_expansion = True
        result.expansion_source_id = original_result.retrieval_id
        return result

    @property
    def index_size(self) -> int:
        return len(self._index)


# 模块级单例
_service: RecipeRetrievalService | None = None


def get_retrieval_service() -> RecipeRetrievalService:
    global _service
    if _service is None:
        _service = RecipeRetrievalService()
        _service.load()
    return _service


def warmup_models() -> None:
    """验证硅基流动嵌入/重排 API 可达（首请求不承担 API 配置错误的冷失败）。

    改 API 后不再加载本地模型；此处做一次真实嵌入 + 重排调用，尽早暴露
    API key 错误 / 模型不可用。
    """
    from food_agent_v2.c1.qdrant_client import _get_embedding_model
    from food_agent_v2.c1.siliconflow import (
        _SILICONFLOW_EMBEDDING_MODEL,
        _SILICONFLOW_RERANK_MODEL,
        record_warmup,
    )

    try:
        _get_embedding_model().encode("预热验证", normalize_embeddings=True)
        record_warmup("embedding", True, _SILICONFLOW_EMBEDDING_MODEL)
        print("[V2] SiliconFlow embedding API: ok")
    except Exception as e:  # noqa: BLE001 —— 预热失败不阻断启动，首请求会再失败
        record_warmup("embedding", False, _SILICONFLOW_EMBEDDING_MODEL)
        print(f"[V2] SiliconFlow embedding API 预热失败 (continuing): {e}")
    try:
        _get_production_reranker().predict([["预热验证", "测试文档"]])
        record_warmup("rerank", True, _SILICONFLOW_RERANK_MODEL)
        print("[V2] SiliconFlow rerank API: ok")
    except Exception as e:  # noqa: BLE001
        record_warmup("rerank", False, _SILICONFLOW_RERANK_MODEL)
        print(f"[V2] SiliconFlow rerank API 预热失败 (continuing): {e}")
