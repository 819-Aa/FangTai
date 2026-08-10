"""C1 Qdrant 向量存储客户端 —— V2 独立 Collection。"""

from __future__ import annotations

from food_agent_v2.core.config import load_config

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

    def __init__(self, collection_name: str | None = None, *, create_if_missing: bool = False):
        cfg = load_config()
        self._host = cfg.qdrant.host
        self._port = cfg.qdrant.rest_port
        self._collection = collection_name or cfg.qdrant.collection
        self._create_if_missing = create_if_missing
        self._client = None
        self._dim = 1024  # BGE-M3

    def _connect(self):
        if self._client is not None:
            return True
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams

            self._client = QdrantClient(host=self._host, port=self._port, timeout=10)
            # The published online name is an alias to the verified physical staging
            # collection.  Treat aliases as existing targets; attempting to create a
            # collection with the alias name makes the online client unavailable.
            collections = {c.name for c in self._client.get_collections().collections}
            aliases = {a.alias_name for a in self._client.get_aliases().aliases}
            target_exists = self._collection in collections or self._collection in aliases
            if not target_exists and self._create_if_missing:
                self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
                )
                target_exists = True
            if not target_exists:
                self._client = None
                return False
            return True
        except Exception:
            self._client = None
            return False

    @property
    def available(self) -> bool:
        return self._connect()

    def index_documents(self, rag_path=None) -> int:
        """直接构建路径已移除（T14/INV-023）。

        固定 RAG 索引只在 H04 隔离初始化事务内构建并发布；请使用
        ``food-agent-v2 data-initialize --manifest ... --confirm-empty-v2``。
        """
        raise RuntimeError(
            "DIRECT_QDRANT_INDEX_REMOVED: use data-initialize for verified indexing"
        )

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


class QdrantInitializationTarget:
    """H04 adapter that builds a physical staging collection and publishes one alias."""

    def __init__(self) -> None:
        cfg = load_config().qdrant
        from qdrant_client import QdrantClient

        self._client = QdrantClient(host=cfg.host, port=cfg.rest_port, timeout=30)
        self._dim = 1024

    def _collections(self) -> set[str]:
        return {item.name for item in self._client.get_collections().collections}

    def _aliases(self) -> dict[str, str]:
        aliases = self._client.get_aliases().aliases
        return {item.alias_name: item.collection_name for item in aliases}

    def target_is_empty(self, collection_name: str) -> bool:
        # A pre-existing empty name is still blocked: initialization must not delete or
        # repurpose a target it did not create.
        return collection_name not in self._collections() and collection_name not in self._aliases()

    def create_staging_collection(self, collection_name: str) -> None:
        from qdrant_client.models import Distance, VectorParams

        if collection_name in self._collections() or collection_name in self._aliases():
            raise RuntimeError(f"staging collection already exists: {collection_name}")
        self._client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
        )

    def index_documents(self, collection_name: str, documents: list[dict]) -> int:
        # The verified manifest already supplies the exact RAG file content.  Reuse the
        # standard embedder while targeting only the explicitly created staging name.
        from qdrant_client.models import PointStruct

        model = _get_embedding_model()
        total = 0
        for start in range(0, len(documents), 32):
            batch = documents[start : start + 32]
            texts = [item["searchable_text"][:512] for item in batch]
            embeddings = model.encode(texts, normalize_embeddings=True)
            points = [
                PointStruct(
                    id=int(document["recipe_id"]),
                    vector=embedding.tolist(),
                    payload={
                        "build_id": document["build_id"],
                        "source_manifest_hash": document["source_manifest_hash"],
                        "document_id": document["document_id"],
                        "name": document["name"],
                        "searchable_fields": document.get("searchable_fields", {}),
                        "step_summary": document.get("step_summary"),
                    },
                )
                for document, embedding in zip(batch, embeddings, strict=True)
            ]
            self._client.upsert(collection_name=collection_name, points=points, wait=True)
            total += len(points)
        return total

    def point_ids(self, collection_name: str) -> set[int]:
        ids: set[int] = set()
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=collection_name,
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids.update(int(point.id) for point in points)
            if offset is None:
                break
        return ids

    def publish_collection(self, staging_name: str, final_name: str) -> None:
        from qdrant_client.models import CreateAlias, CreateAliasOperation

        self._client.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    create_alias=CreateAlias(
                        collection_name=staging_name,
                        alias_name=final_name,
                    )
                )
            ]
        )

    def delete_collection(self, collection_name: str) -> None:
        aliases = self._aliases()
        if collection_name in aliases:
            from qdrant_client.models import DeleteAlias, DeleteAliasOperation

            self._client.update_collection_aliases(
                change_aliases_operations=[
                    DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=collection_name))
                ]
            )
        if collection_name in self._collections():
            self._client.delete_collection(collection_name=collection_name)
