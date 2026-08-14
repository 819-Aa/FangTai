"""硅基流动（SiliconFlow）嵌入与重排 API 封装。

替代本地 SentenceTransformer / CrossEncoder 的 BGE 模型加载（CPU 冷加载
2-4 分钟），改为调用 SiliconFlow 的 OpenAI 兼容 embeddings / rerank API。
接口与本地模型保持兼容：encode 返回 numpy array、predict 返回 list[float]。
"""

from __future__ import annotations

import json
import os
import urllib.request

import numpy as np

_SILICONFLOW_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
_SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
_SILICONFLOW_EMBEDDING_MODEL = os.getenv("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3")
_SILICONFLOW_RERANK_MODEL = os.getenv("SILICONFLOW_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")


class SiliconFlowError(RuntimeError):
    """硅基流动 API 调用失败。"""


def _post(path: str, body: dict, timeout: int = 60) -> dict:
    """POST 到 SiliconFlow API，返回解析后的 JSON。"""
    if not _SILICONFLOW_API_KEY:
        raise SiliconFlowError("SILICONFLOW_API_KEY 未配置")
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{_SILICONFLOW_BASE_URL}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_SILICONFLOW_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        raise SiliconFlowError(f"SiliconFlow API {path} 失败 {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise SiliconFlowError(f"SiliconFlow API {path} 连接失败: {e.reason}") from e


class SiliconFlowEmbedder:
    """BGE-M3 嵌入（SiliconFlow API），接口兼容 SentenceTransformer.encode。"""

    def encode(self, texts, normalize_embeddings: bool = True, **kwargs):
        single = isinstance(texts, str)
        inputs = [texts] if single else list(texts)
        if not inputs:
            return np.zeros((0, 1024), dtype=np.float32)

        resp = _post(
            "/embeddings",
            {"model": _SILICONFLOW_EMBEDDING_MODEL, "input": inputs},
        )
        data = resp.get("data", [])
        if len(data) != len(inputs):
            raise SiliconFlowError(
                f"嵌入返回数量不匹配: 期望 {len(inputs)}，实际 {len(data)}")
        # 按 index 排序（SiliconFlow 返回顺序应与 input 一致，但稳妥起见按 index）
        data_sorted = sorted(data, key=lambda d: d.get("index", 0))
        arr = np.array([d["embedding"] for d in data_sorted], dtype=np.float32)

        if normalize_embeddings:
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            arr = arr / np.maximum(norms, 1e-8)
        return arr[0] if single else arr


class SiliconFlowReranker:
    """BGE-Reranker-v2-m3 重排（SiliconFlow API），接口兼容 CrossEncoder.predict。

    predict(pairs) 接收 [query, doc] 对列表；本实现按调用点语义（所有 pair 的
    query 相同）拆成一次 query + documents 调用，再按 index 映射回分数。
    """

    def predict(self, pairs: list[list[str]]) -> list[float]:
        if not pairs:
            return []
        query = pairs[0][0]
        documents = [p[1] for p in pairs]

        resp = _post(
            "/rerank",
            {"model": _SILICONFLOW_RERANK_MODEL, "query": query, "documents": documents},
        )
        results = resp.get("results", [])
        if len(results) != len(documents):
            raise SiliconFlowError(
                f"重排返回数量不匹配: 期望 {len(documents)}，实际 {len(results)}")
        scores = [0.0] * len(documents)
        for r in results:
            idx = r.get("index", 0)
            if 0 <= idx < len(scores):
                scores[idx] = float(r.get("relevance_score", 0.0))
        return scores
