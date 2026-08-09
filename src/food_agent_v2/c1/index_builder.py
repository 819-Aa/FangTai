"""C1 向量索引批量构建 —— BGE-M3 嵌入 2000 条 RAG 文档 → Qdrant。"""

from __future__ import annotations

import json
from pathlib import Path

from food_agent_v2.core.paths import CLEANED_DIR
from food_agent_v2.c1.qdrant_client import QdrantVectorStore


def build_index(rag_path: Path | None = None, batch_size: int = 32) -> dict:
    """批量生成 BGE-M3 嵌入并写入 Qdrant。

    在首次调用时加载 BGE-M3 模型（~2GB），后续批次复用。
    """
    if rag_path is None:
        rag_path = CLEANED_DIR / "rag_documents.jsonl"

    if not rag_path.exists():
        return {"status": "failed", "reason": "RAG documents not found"}

    # 加载文档
    docs = []
    with rag_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                docs.append(json.loads(line))

    store = QdrantVectorStore()
    if not store.available:
        return {"status": "failed", "reason": "Qdrant not available"}

    total = store.index_documents(rag_path)
    return {
        "status": "done",
        "total_documents": len(docs),
        "total_indexed": total,
    }


if __name__ == "__main__":
    result = build_index()
    print(json.dumps(result, ensure_ascii=False, indent=2))
