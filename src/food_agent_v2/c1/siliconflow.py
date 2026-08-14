"""硅基流动（SiliconFlow）嵌入与重排 API 封装（线程安全连接池版）。

替代本地 SentenceTransformer / CrossEncoder 的 BGE 模型加载，调用 SiliconFlow
OpenAI 兼容 embeddings / rerank API。接口兼容本地模型：encode 返回 numpy array、
predict 返回 list[float]。

P2 修复：进程级 httpx 连接池（线程安全），替换模块级单一 http.client 连接——
并发请求下单一 HTTPConnection 会抛 ``CannotSendRequest``，httpx.Client 内部连接池
支持并发 + keep-alive。
"""

from __future__ import annotations

import os

import httpx
import numpy as np

_SILICONFLOW_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
_SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
_SILICONFLOW_EMBEDDING_MODEL = os.getenv("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3")
_SILICONFLOW_RERANK_MODEL = os.getenv("SILICONFLOW_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")


class SiliconFlowError(RuntimeError):
    """硅基流动 API 调用失败。"""


class SiliconFlowHTTPClient:
    """进程级 httpx 客户端（线程安全连接池），封装 SiliconFlow POST。

    HTTP 4xx/5xx 与连接/超时错误统一转为不含 API key 和完整供应商正文的
    ``SiliconFlowError``；每次请求传入本轮剩余预算形成的 timeout，不做隐式重试。
    """

    def __init__(self, api_key: str, base_url: str):
        self._api_key = api_key
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    def post(self, path: str, body: dict, timeout_seconds: float = 60.0) -> dict:
        if not self._api_key:
            raise SiliconFlowError("SILICONFLOW_API_KEY 未配置")
        try:
            resp = self._client.post(
                path, json=body, timeout=httpx.Timeout(timeout_seconds))
        except httpx.TimeoutException as exc:
            raise SiliconFlowError(
                f"SiliconFlow API {path} 超时（>{timeout_seconds}s）") from exc
        except httpx.RequestError as exc:
            raise SiliconFlowError(
                f"SiliconFlow API {path} 连接失败: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            detail = (resp.text or "")[:200]
            raise SiliconFlowError(
                f"SiliconFlow API {path} 失败 {resp.status_code}: {detail}")
        try:
            return resp.json()
        except ValueError:
            return {}

    def close(self) -> None:
        self._client.close()


#: 进程级共享客户端（线程安全连接池）。
_client: SiliconFlowHTTPClient | None = None


def get_siliconflow_http_client() -> SiliconFlowHTTPClient:
    global _client
    if _client is None:
        _client = SiliconFlowHTTPClient(_SILICONFLOW_API_KEY, _SILICONFLOW_BASE_URL)
    return _client


def close_siliconflow_http_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


#: warmup 探测状态（/ready fail-closed 用），只存脱敏供应商/模型身份。
_warmup: dict[str, dict] = {}


def record_warmup(kind: str, ok: bool, model: str) -> None:
    _warmup[kind] = {"status": "ready" if ok else "unavailable", "model": model}


def warmup_status() -> dict:
    return dict(_warmup)


class SiliconFlowEmbedder:
    """BGE-M3 嵌入（SiliconFlow API），接口兼容 SentenceTransformer.encode。"""

    def encode(self, texts, normalize_embeddings: bool = True, **kwargs):
        single = isinstance(texts, str)
        inputs = [texts] if single else list(texts)
        if not inputs:
            return np.zeros((0, 1024), dtype=np.float32)

        resp = get_siliconflow_http_client().post(
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

        resp = get_siliconflow_http_client().post(
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
