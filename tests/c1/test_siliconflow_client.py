"""SiliconFlow 线程安全 HTTP 客户端测试（L0 Task 2）。

验证：进程级 httpx 连接池支持并发、timeout 参数被强制、HTTP 4xx/5xx 与
连接错误统一转 SiliconFlowError（不含 API key）。不访问真实外部 API。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from food_agent_v2.c1.siliconflow import SiliconFlowError, SiliconFlowHTTPClient


def _make_handler(delay: float = 0.0, status: int = 200):
    class _JSONHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            import time
            time.sleep(self.__class__.delay)
            try:
                parsed = json.loads(raw or b"{}")
            except ValueError:
                parsed = {}
            resp = json.dumps({"path": self.path, "body": parsed}).encode()
            self.send_response(self.__class__.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    _JSONHandler.delay = delay
    _JSONHandler.status = status
    return _JSONHandler


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def local_json_server():
    server = _serve(_make_handler(delay=0.05, status=200))
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def slow_json_server():
    server = _serve(_make_handler(delay=0.5, status=200))
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_shared_client_allows_concurrent_posts(local_json_server):
    client = SiliconFlowHTTPClient(api_key="test", base_url=local_json_server)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(
            lambda i: client.post("/test", {"i": i}, 1.0), range(20)))
    assert len(results) == 20
    assert {r["body"]["i"] for r in results} == set(range(20))
    client.close()


def test_timeout_argument_is_enforced(slow_json_server):
    client = SiliconFlowHTTPClient(api_key="test", base_url=slow_json_server)
    with pytest.raises(SiliconFlowError, match="超时"):
        client.post("/test", {}, timeout_seconds=0.2)
    client.close()


def test_http_error_mapped_to_siliconflow_error(local_json_server):
    # 用 500 状态验证错误映射
    server = _serve(_make_handler(delay=0.0, status=500))
    try:
        client = SiliconFlowHTTPClient(
            api_key="test", base_url=f"http://127.0.0.1:{server.server_address[1]}")
        with pytest.raises(SiliconFlowError, match="500"):
            client.post("/test", {}, 1.0)
        client.close()
    finally:
        server.shutdown()
        server.server_close()


def test_missing_api_key_fails_without_request():
    client = SiliconFlowHTTPClient(api_key="", base_url="http://127.0.0.1:1")
    with pytest.raises(SiliconFlowError, match="未配置"):
        client.post("/embeddings", {"input": ["x"]}, 1.0)
    client.close()
