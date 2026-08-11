"""T20 API 应用集成测试：SSE 稳定 ID 续传、/users 匿名隔离、取消/失败不发成功事件。

依赖 MySQL/Redis 可用（请求/取消真实存储）。TestClient 不进入 lifespan 以跳过模型预热。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from food_agent_v2.api_app import app
from food_agent_v2.d1 import api
from food_agent_v2.d1.schemas import SSEEventType


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def client():
    # 不进入 lifespan（跳过 RAG 模型预热）
    c = TestClient(app)
    yield c
    c.close()


class TestApiApplication:
    def test_users_endpoint_is_anonymous(self, client) -> None:
        """公开 /users 不得暴露真实用户/健康档案。"""
        resp = client.get("/v1/users")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert not any(k in body for k in ("allergies", "diseases", "age"))

    def test_sse_stream_resumes_from_string_id(self) -> None:
        """Last-Event-ID=ev_answer_xxx → 端点生成器首个块即 result_committed（不重放 answer_ready）。

        直接调用路由端点读取生成器首块（本环境 TestClient/httpx 对无限 SSE 流式传输
        存在缓冲限制，故不经 HTTP 长连接；生成器首块即时 yield，验证修复本身）。
        """
        import asyncio
        from types import SimpleNamespace

        rid = _unique("r")
        api._requests[rid] = {"request_id": rid, "status": "running", "session_id": "s"}
        api.publish_answer_event(rid, "菜单", "m", ["e"], event_id=f"ev_answer_{rid}")
        api.publish_result_committed(rid, {"plan_id": "p"}, event_id=f"ev_result_{rid}")

        endpoint = next(r.endpoint for r in app.routes
                        if getattr(r, "path", "") == "/v1/recommendation-requests/{request_id}/events")

        async def first_chunk() -> str:
            req = SimpleNamespace(headers={"Last-Event-ID": f"ev_answer_{rid}"})
            resp = await endpoint(rid, req)
            assert resp.media_type == "text/event-stream"
            it = resp.body_iterator.__aiter__()
            first = await it.__anext__()
            await it.aclose()
            return first

        first = asyncio.run(asyncio.wait_for(first_chunk(), timeout=10))
        assert f"ev_result_{rid}" in first
        assert "event: result_committed" in first
        assert "event: answer_ready" not in first

    def test_cancel_emits_cancel_not_success(self) -> None:
        """取消 → 只发 request_cancelled，绝无 answer_ready/result_committed。"""
        rid = _unique("r")
        api._requests[rid] = {"request_id": rid, "status": "running", "session_id": "s"}
        code, _ = api.cancel_request(rid)
        assert code == 200
        types = [e["event"] for e in api.subscribe_events(rid)]
        assert "request_cancelled" in types
        assert "answer_ready" not in types
        assert "result_committed" not in types

    def test_failure_emits_error_not_success(self) -> None:
        """失败 → 只发 error，绝无 answer_ready/result_committed。"""
        rid = _unique("r")
        api._requests[rid] = {"request_id": rid, "status": "failed", "session_id": "s"}
        api._emit_event(rid, SSEEventType.ERROR, {"error_code": "WORKFLOW_ERROR"})
        types = [e["event"] for e in api.subscribe_events(rid)]
        assert "error" in types
        assert "answer_ready" not in types
        assert "result_committed" not in types

    def test_get_status_stage_cursor_is_integer(self) -> None:
        """GET 状态中 stage_events_cursor 始终符合 integer 契约（即使事件用字符串 ID）。"""
        rid = _unique("r")
        api._requests[rid] = {"request_id": rid, "status": "running", "session_id": "s",
                              "created_at": "t", "stage_events_cursor": 0}
        api.publish_answer_event(rid, "菜单", "m", ["e"], event_id=f"ev_answer_{rid}")
        code, resp = api.get_request_status(rid)
        assert code == 200
        assert isinstance(resp["stage_events_cursor"], int)
