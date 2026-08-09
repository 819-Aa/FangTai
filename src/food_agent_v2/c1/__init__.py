"""C1 混合 RAG 检索 —— 词法(BM25)+向量(BGE-M3→Qdrant)+RRF+重排(BGE-Reranker)。

检索链路不降级：向量检索引擎不可用时整体 failed（生产模式）。
开发/离线模式可通过 QDRANT_REQUIRED=false 切换为纯词法。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from food_agent_v2.core.paths import CLEANED_DIR
from food_agent_v2.c1.qdrant_client import QdrantVectorStore


RRF_K = 60
# 加权 RRF 权重（文档 07 §7.3：词法 0.3 / 向量 0.7）
LEXICAL_WEIGHT = 0.3
VECTOR_WEIGHT = 0.7

# 重排器缓存：BGE-Reranker-v2-M3（文档 07 §9.5），避免每次检索重新加载
_reranker = None


def _get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder("BAAI/bge-reranker-v2-m3", cache_folder=".model-cache")
    return _reranker


# ---- Data classes (unchanged) ----

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


# ---- BM25 (unchanged) ----

class BM25Index:
    """简化 BM25 索引。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: dict[int, dict] = {}            # recipe_id → doc
        self._doc_lengths: dict[int, int] = {}
        self._avg_dl: float = 0
        self._idf: dict[str, float] = {}            # term → IDF
        self._term_freqs: dict[str, dict[int, int]] = defaultdict(dict)  # term → {doc_id → tf}

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中文混合分词：字符2-gram + 单字。"""
        text = text.lower().strip()
        tokens = []
        # 2-gram
        for i in range(len(text) - 1):
            tokens.append(text[i:i+2])
        # 单字
        tokens.extend(list(text))
        return tokens

    def index(self, docs: list[dict]) -> None:
        """构建索引。"""
        self._docs.clear()
        self._term_freqs.clear()

        for doc in docs:
            rid = doc["recipe_id"]
            self._docs[rid] = doc
            text = doc.get("searchable_text", doc.get("name", ""))
            tokens = self._tokenize(text)
            self._doc_lengths[rid] = len(tokens)

            term_counts = defaultdict(int)
            for t in tokens:
                term_counts[t] += 1
            for term, count in term_counts.items():
                self._term_freqs[term][rid] = count

        # 计算 IDF
        n_docs = len(self._docs)
        if n_docs == 0:
            return
        self._avg_dl = sum(self._doc_lengths.values()) / n_docs

        for term, doc_freqs in self._term_freqs.items():
            df = len(doc_freqs)
            self._idf[term] = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)

    def search(self, query: str, top_k: int = 50) -> list[tuple[int, float]]:
        """BM25 搜索。"""
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

        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return ranked

    def __len__(self) -> int:
        return len(self._docs)


class RecipeRetrievalService:
    """菜品混合检索服务 —— BM25 + Qdrant 向量 + RRF + 重排。

    生产模式（QDRANT_REQUIRED=true）：向量不可用 → 整体 failed。
    离线模式（QDRANT_REQUIRED=false）：降级为纯 BM25。
    """

    def __init__(self):
        self._index = BM25Index()
        self._vector = QdrantVectorStore()
        self._loaded = False
        self._vector_required = os.getenv("QDRANT_REQUIRED", "true").lower() == "true"
        self._time_lookup: dict[int, dict] = {}   # recipe_id → {total_minutes, confidence}

    def load(self, rag_path: Optional[Path] = None) -> None:
        if rag_path is None:
            rag_path = CLEANED_DIR / "rag_documents.jsonl"
        if not rag_path.exists():
            return
        docs = []
        with rag_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    docs.append(json.loads(line))
        self._index.index(docs)

        # 加载时间查询表（time_profiles 的 LLM 估算）供 time_boost 使用（文档 07 §9.6）
        self._time_lookup = {}
        tp_path = CLEANED_DIR / "time_profiles.jsonl"
        if tp_path.exists():
            with tp_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rec = json.loads(line)
                        est = rec.get("llm_estimate")
                        if est and est.get("total_minutes"):
                            self._time_lookup[rec["recipe_id"]] = {
                                "total_minutes": est.get("total_minutes"),
                                "confidence": est.get("confidence", "low"),
                            }
        self._loaded = True

    def retrieve(
        self, query: str, top_k: int = 20, exclude_ids: list[int] | None = None
    ) -> RetrievalResult:
        """混合检索：BM25 + 向量 → RRF 融合 → 重排。"""
        if not self._loaded:
            return RetrievalResult(retrieval_id="empty", request_id=None,
                                   candidates=[], total_candidates=0, retrieval_path="empty")

        exclude = set(exclude_ids or [])

        # 1. 词法检索 (BM25)
        lexical_ranked = self._index.search(query, top_k=100)
        lexical_ranked = [(rid, s) for rid, s in lexical_ranked if rid not in exclude]

        # 2. 向量检索 (Qdrant + BGE-M3)
        vector_ok = self._vector.available
        vector_ranked: list[tuple[int, float]] = []
        if vector_ok:
            vector_ranked = self._vector.search(query, top_k=100)
            vector_ranked = [(rid, s) for rid, s in vector_ranked if rid not in exclude]
        elif self._vector_required:
            return RetrievalResult(retrieval_id="failed", request_id=None,
                                   candidates=[], total_candidates=0,
                                   retrieval_path="vector_unavailable")

        # 3. RRF 融合
        fused = self._rrf_fuse(lexical_ranked, vector_ranked if vector_ok else [])
        retrieval_path = "hybrid" if vector_ok else "lexical"

        # 归一化（重排前取稍大候选集：融合 Top-30 → 重排 → Top-k）
        candidates = self._normalize_and_build(fused, max(top_k, 30))

        # 4. BGE-Reranker-v2-M3 重排（文档 07 §9.5：完整链路必须包含重排）
        candidates = self._rerank(query, candidates, top_k)
        if retrieval_path == "hybrid":
            retrieval_path = "hybrid_rerank"

        # 5. time_boost 软偏置（文档 07 §9.6：查询含时间短语义时高置信短时菜提前）
        candidates = self._apply_time_boost(query, candidates, top_k)

        return RetrievalResult(
            retrieval_id=f"retr_{len(candidates)}", request_id=None,
            candidates=candidates, total_candidates=len(candidates),
            retrieval_path=retrieval_path,
            source_paths=[retrieval_path],
        )

    def _rrf_fuse(
        self, lexical: list[tuple[int, float]], vector: list[tuple[int, float]]
    ) -> list[tuple[int, float, float, float]]:
        """加权 RRF 融合（文档 07 §9.4）：RRF = Σ weight_s/(k + rank_s)。"""
        scores: dict[int, dict[str, float]] = defaultdict(lambda: {"lex": 0.0, "vec": 0.0})

        for rank, (rid, _) in enumerate(lexical):
            scores[rid]["lex"] = LEXICAL_WEIGHT / (RRF_K + rank + 1)
        for rank, (rid, _) in enumerate(vector):
            scores[rid]["vec"] = VECTOR_WEIGHT / (RRF_K + rank + 1)

        fused = []
        for rid, s in scores.items():
            fused_score = s["lex"] + s["vec"]
            fused.append((rid, fused_score, s["lex"], s["vec"]))
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
            candidates.append(RetrievalCandidate(
                recipe_id=rid,
                document_id=doc.get("document_id", f"recipe_{rid:04d}"),
                name=doc.get("name", ""),
                score=round((fused - min_s) / score_range, 4),
                lexical_score=round(lex_s, 4),
                vector_score=round(vec_s, 4),
                searchable_fields={k: v for k, v in fields.items()},
                source_paths=[f"rrf:rank:{i+1}"],
            ))
        return candidates

    def _candidate_rerank_text(self, c: RetrievalCandidate) -> str:
        """构建重排用的文档文本：菜名 + 检索字段。"""
        fields = c.searchable_fields or {}
        extra = " ".join(str(v) for v in fields.values() if v)
        return f"{c.name} {extra}".strip()

    def _rerank(
        self, query: str, candidates: list[RetrievalCandidate], top_k: int
    ) -> list[RetrievalCandidate]:
        """BGE-Reranker-v2-M3 重排（文档 07 §9.5）。

        只改变候选顺序，不引入过滤或安全判定。
        重排器加载/推理失败时回退到融合排序（不崩溃）。
        """
        if not candidates:
            return candidates
        # RERANKER_ENABLED=false 时跳过重排（开发/快速模式），默认开启
        if os.getenv("RERANKER_ENABLED", "true").lower() == "false":
            return candidates[:top_k]
        try:
            reranker = _get_reranker()
            pairs = [[query, self._candidate_rerank_text(c)] for c in candidates]
            scores = reranker.predict(pairs)
        except Exception:
            return candidates[:top_k]  # 加载/推理失败时回退融合排序，不崩溃
        ranked = sorted(zip(candidates, scores), key=lambda x: -x[1])
        result = []
        for c, s in ranked[:top_k]:
            c.rerank_score = float(s)
            result.append(c)
        return result

    def _apply_time_boost(
        self, query: str, candidates: list[RetrievalCandidate], top_k: int
    ) -> list[RetrievalCandidate]:
        """文档 07 §9.6：查询含时间短语义时，对高置信度短时菜施加软偏置 ×1.15。

        仅改变排序位置，不排除任何菜品，不用于严格时间判断。
        用户未提及时间偏好时不应用。
        """
        time_kws = ("快手", "半小时", "30分钟", "快速", "快一点", "时间短",
                    "省时", "简便", "简单快", "时间紧", "赶时间", "尽快")
        if not any(kw in query for kw in time_kws):
            return candidates
        for c in candidates:
            info = self._time_lookup.get(c.recipe_id)
            if info and info.get("confidence") == "high" \
                    and (info.get("total_minutes") or 999) <= 30:
                c.rerank_score = (c.rerank_score or c.score) * 1.15
        candidates.sort(
            key=lambda x: -(x.rerank_score if x.rerank_score is not None else x.score))
        return candidates[:top_k]

    def multi_person_retrieve(
        self, shared_query: str,
        per_participant_prefs: list[list[str]],
        top_k: int = 50,
    ) -> RetrievalResult:
        """多人多路检索（文档 07 §8.4）。

        共享需求 + 每位参与者的正向口味偏好 → 各路完整混合检索
        → 合并去重（记录 source_paths）→ 取前 multi_person_recall_limit=50。
        """
        merged: dict[int, RetrievalCandidate] = {}

        # 1. 共享查询
        shared = self.retrieve(shared_query, top_k=min(top_k, 30))
        for c in shared.candidates:
            c.source_paths = ["shared"]
            merged[c.recipe_id] = c

        # 2. 每参与者偏好子查询
        for i, prefs in enumerate(per_participant_prefs):
            if not prefs:
                continue
            sub_query = f"{shared_query} {' '.join(prefs[:3])}".strip()
            sub = self.retrieve(sub_query, top_k=min(top_k, 20))
            for c in sub.candidates:
                if c.recipe_id in merged:
                    if f"participant_{i + 1}" not in merged[c.recipe_id].source_paths:
                        merged[c.recipe_id].source_paths.append(f"participant_{i + 1}")
                else:
                    c.source_paths = [f"participant_{i + 1}"]
                    merged[c.recipe_id] = c

        # 3. 按融合分降序，取前 top_k
        cands = sorted(merged.values(), key=lambda c: -c.score)[:top_k]
        paths = ["shared"] + [f"participant_{i + 1}"
                              for i in range(len(per_participant_prefs))]
        return RetrievalResult(
            retrieval_id=f"multi_{len(cands)}",
            request_id=None,
            candidates=cands,
            total_candidates=len(cands),
            retrieval_path="multi_person_hybrid_rerank",
            source_paths=paths,
        )

    def expand_retrieval(
        self, original_result: RetrievalResult, query_plan: dict
    ) -> RetrievalResult | None:
        """扩展召回——使用 relaxation 策略再跑一次。"""
        if not self._loaded:
            return None
        original_ids = {c.recipe_id for c in original_result.candidates}
        # 使用更宽松的查询
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
    """预热 BGE-M3 嵌入与 BGE-Reranker-v2-M3（文档 07 §13：启动预热，首轮不承担冷加载）。

    RERANKER_ENABLED=false 时只预热嵌入模型。
    """
    from food_agent_v2.c1.qdrant_client import _get_embedding_model
    _get_embedding_model()
    if os.getenv("RERANKER_ENABLED", "true").lower() != "false":
        _get_reranker()
