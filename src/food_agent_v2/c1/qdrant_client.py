"""C1 Qdrant 向量存储客户端 —— V2 独立 Collection。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from food_agent_v2.core.config import load_config
from food_agent_v2.core.paths import CLEANED_DIR


# 模块级 BGE-M3 模型缓存：避免每次检索都重新加载 ~2GB 模型（文档 07 §13：启动预热，首轮不承担冷加载）
_embedding_model = None


def _get_embedding_model():
    """返回缓存的 BGE-M3 嵌入模型（首次调用时加载）。"""
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer("BAAI/bge-m3", cache_folder=".model-cache")
    return _embedding_model


class QdrantVectorStore:
    """Qdrant 向量存储（v2 collection 隔离）。"""

    def __init__(self):
        cfg = load_config()
        self._host = cfg.qdrant.host
        self._port = cfg.qdrant.rest_port
        self._collection = cfg.qdrant.collection
        self._client = None
        self._dim = 1024  # BGE-M3

    def _connect(self):
        if self._client is not None:
            return True
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
            self._client = QdrantClient(host=self._host, port=self._port, timeout=10)
            # 确保 collection 存在
            collections = [c.name for c in self._client.get_collections().collections]
            if self._collection not in collections:
                self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
                )
            return True
        except Exception:
            self._client = None
            return False

    @property
    def available(self) -> bool:
        return self._connect()

    def index_documents(self, rag_path: Path | None = None) -> int:
        """将 RAG 文档生成向量并写入 Qdrant。"""
        if rag_path is None:
            rag_path = CLEANED_DIR / "rag_documents.jsonl"
        if not rag_path.exists():
            return 0
        if not self._connect():
            return 0

        docs = []
        with rag_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    docs.append(json.loads(line))

        # 分批嵌入
        from qdrant_client.models import PointStruct
        model = _get_embedding_model()

        total = 0
        for i in range(0, len(docs), 32):
            batch = docs[i:i+32]
            texts = [d["searchable_text"][:512] for d in batch]
            embeddings = model.encode(texts, normalize_embeddings=True)

            points = [
                PointStruct(
                    id=d["recipe_id"],
                    vector=emb.tolist(),
                    payload={
                        "document_id": d["document_id"],
                        "name": d["name"],
                        "searchable_fields": d.get("searchable_fields", {}),
                        "step_summary": d.get("step_summary"),
                    },
                )
                for d, emb in zip(batch, embeddings)
            ]
            self._client.upsert(collection_name=self._collection, points=points)
            total += len(points)

        return total

    def search(self, query_text: str, top_k: int = 50) -> list[tuple[int, float]]:
        """向量检索（BGE-M3 嵌入 + Qdrant 搜索）。

        使用 qdrant-client >= 1.19 的 query_points 接口
        （旧版 QdrantClient.search 在 1.19 中已被移除）。
        """
        if not self._connect():
            return []

        model = _get_embedding_model()
        embedding = model.encode(query_text, normalize_embeddings=True)

        results = self._client.query_points(
            collection_name=self._collection,
            query=embedding.tolist(),
            limit=top_k,
        )
        return [(hit.id, hit.score) for hit in (results.points or [])]
