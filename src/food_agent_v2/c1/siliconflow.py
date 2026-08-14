"""硅基流动（SiliconFlow）嵌入与重排 API 封装。

替代本地 SentenceTransformer / CrossEncoder 的 BGE 模型加载（CPU 冷加载
2-4 分钟），改为调用 SiliconFlow 的 OpenAI 兼容 embeddings / rerank API。
接口与本地模型保持兼容：encode 返回 numpy array、predict 返回 list[float]。
"""

from __future__ import annotations

import http.client
import json
import os
from urllib.parse import urlparse

import numpy as np

_SILICONFLOW_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
_SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
_SILICONFLOW_EMBEDDING_MODEL = os.getenv("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3")
_SILICONFLOW_RERANK_MODEL = os.getenv("SILICONFLOW_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")


class SiliconFlowError(RuntimeError):
    """硅基流动 API 调用失败。"""


# P2：进程生命周期复用的持久 HTTP(S) 连接（keep-alive），
# 避免每次嵌入/重排调用重新 TLS 握手。连接失效由 _post 置 None 后重建。
_conn: http.client.HTTPConnection | None = None
# base_url 的路径前缀（如 https://api.siliconflow.cn/v1 → /v1）；http.client 的
# 连接只承载 host:port，路径前缀需在请求时拼接（否则漏 /v1 会 404）。
_BASE_PATH = urlparse(_SILICONFLOW_BASE_URL).path.rstrip("/")


def _get_connection() -> http.client.HTTPConnection:
    global _conn
    if _conn is not None:
        return _conn
    parsed = urlparse(_SILICONFLOW_BASE_URL)
    cls = (http.client.HTTPSConnection if parsed.scheme == "https"
           else http.client.HTTPConnection)
    _conn = cls(parsed.hostname, parsed.port, timeout=60)
    return _conn


def _post(path: str, body: dict, timeout: int = 60) -> dict:
    """POST 到 SiliconFlow API，返回解析后的 JSON。

    复用模块级持久连接（keep-alive，固定 60s 超时；``timeout`` 参数保留兼容）。
    半开连接/对端关闭等连接级错误重建重试一次；HTTP 4xx/5xx 按 SiliconFlowError
    抛出（不重试）。
    """
    if not _SILICONFLOW_API_KEY:
        raise SiliconFlowError("SILICONFLOW_API_KEY 未配置")
    data = json.dumps(body, ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_SILICONFLOW_API_KEY}",
    }
    global _conn
    for attempt in (0, 1):
        conn = _get_connection()
        try:
            conn.request("POST", _BASE_PATH + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()  # 必须读完整 body，连接才能复用
            payload = json.loads(raw) if raw else {}
            if resp.status >= 400:
                detail = str(payload)[:200] if payload else ""
                raise SiliconFlowError(
                    f"SiliconFlow API {path} 失败 {resp.status}: {detail}")
            return payload
        except SiliconFlowError:
            raise
        except (http.client.HTTPException, OSError, ConnectionError) as e:
            # 连接失效（半开/对端关闭）→ 关闭并重建，重试一次
            _conn = None
            try:
                conn.close()
            except Exception:
                pass
            if attempt == 0:
                continue
            raise SiliconFlowError(f"SiliconFlow API {path} 连接失败: {e}") from e


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
