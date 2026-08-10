"""T14 C1 真实 Qdrant 集成检索测试（需要 H04 隔离环境启动且已初始化）。

验证 recipe_retrieval_v2 别名存在、真实向量检索返回 recipe_id、服务能从
固定 Repository 加载（BM25 + eligible 集合 + 向量可用）。
"""

from food_agent_v2.c1 import RecipeRetrievalService
from food_agent_v2.c1.qdrant_client import QdrantVectorStore
from food_agent_v2.core.config import load_config


def test_qdrant_alias_exists() -> None:
    import qdrant_client

    store = QdrantVectorStore()
    assert store.available, "Qdrant recipe_retrieval_v2 别名不可用（H04 环境未启动/未初始化）"
    client = qdrant_client.QdrantClient(
        host=load_config().qdrant.host,
        port=load_config().qdrant.rest_port,
        timeout=10,
    )
    aliases = {a.alias_name: a.collection_name for a in client.get_aliases().aliases}
    assert "recipe_retrieval_v2" in aliases, "缺少 recipe_retrieval_v2 别名"


def test_real_vector_search_returns_ids() -> None:
    store = QdrantVectorStore()
    results = store.search("红烧肉", top_k=5)
    assert results, "真实向量检索应返回候选"
    assert all(isinstance(rid, int) for rid, _ in results)


def test_retrieval_service_loads_from_fixed_repository() -> None:
    service = RecipeRetrievalService()
    service.load()
    assert service.index_size > 0, "BM25 索引应为空（rag_documents 未加载）"
    assert service.build_id is not None
    assert service._vector.available
    assert len(service._eligible) > 0
