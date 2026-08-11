"""T20 API 应用集成测试：SSE 稳定 ID 续传、/users 匿名隔离、取消/失败不发成功事件、
错误 Schema、游标递增、禁止字段投影、幂等持久、真实 session 查询。

依赖 MySQL/Redis 可用（请求/取消/session/outbox 真实存储）。
TestClient 不进入 lifespan 以跳过模型预热。
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from food_agent_v2.api_app import app
from food_agent_v2.d1 import SensitiveDataBlocked, api
from food_agent_v2.d1.schemas import SSEEventType


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _delete_redis_keys(*redis_keys: str) -> None:
    """清理测试产生的 Redis 幂等键/request key（T20 F）。"""
    try:
        from food_agent_v2.c4.redis_store import RedisSessionStore
        store = RedisSessionStore()
        store._connect()
        if store._client:
            for k in redis_keys:
                store._client.delete(k)
    except Exception:
        pass


def _delete_mysql_session(session_id: str) -> None:
    """清理测试产生的 MySQL session 行（T20 F）。"""
    try:
        import pymysql

        from food_agent_v2.core.config import load_config
        cfg = load_config().mysql
        conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                               password=cfg.password, database=cfg.database,
                               charset="utf8mb4")
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE session_id=%s", (session_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _mysql_available() -> bool:
    try:
        import pymysql

        from food_agent_v2.core.config import load_config
        cfg = load_config()
        conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                               user=cfg.mysql.user, password=cfg.mysql.password,
                               database=cfg.mysql.database, charset="utf8mb4",
                               connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def client():
    # 不进入 lifespan（跳过 RAG 模型预热）
    c = TestClient(app)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _suppress_workflow(monkeypatch):
    """避免 create_request 触发真实工作流线程。"""
    monkeypatch.setattr(api, "_trigger_workflow", lambda *a, **k: None)
    yield


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

    # ---- 契约缺口回归（Codex 黑盒验证）----

    def test_invalid_json_returns_422_top_level(self, client) -> None:
        """非法 JSON → 顶层 422 VALIDATION_FAILED（不 500、不包 detail）。"""
        resp = client.post("/v1/recommendation-requests", content="{not-json")
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "VALIDATION_FAILED"
        assert "detail" not in body
        assert any(e["field"] == "body" for e in body["details"])

    def test_error_schema_is_top_level(self, client) -> None:
        """422/404 错误结构在顶层，不包在 detail 中。"""
        resp = client.post("/v1/recommendation-requests",
                           json={"message": "x",
                                 "participants": [{"participant_ref": "p1"}]})
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "VALIDATION_FAILED"
        assert "details" in body and "detail" not in body

        resp = client.get(f"/v1/recommendation-requests/{_unique('missing')}")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "NOT_FOUND"
        assert "detail" not in body

    def test_analysis_event_payload_is_real_data(self) -> None:
        """analysis_ready 的 data 为原始 stage/summary/evidence_refs（非 violations 列表）。"""
        rid = _unique("r")
        api._requests[rid] = {"request_id": rid, "status": "running"}
        api.publish_analysis_event(rid, "health", "分析了 10 道菜", ["ev:1"])
        ev = next(e for e in api.subscribe_events(rid) if e["event"] == "analysis_ready")
        data = json.loads(ev["data"])
        assert data["stage"] == "health"
        assert data["summary"] == "分析了 10 道菜"
        assert data["evidence_refs"] == ["ev:1"]
        assert isinstance(data, dict)

    def test_stage_cursor_increments_on_every_new_event(self) -> None:
        """每个首次发布事件（含稳定字符串 id）递增游标；重复 event_id 不递增。"""
        rid = _unique("r")
        api._requests[rid] = {"request_id": rid, "status": "running",
                              "session_id": "s", "created_at": "t",
                              "stage_events_cursor": 0}
        api._emit_event(rid, SSEEventType.REQUEST_ACCEPTED, {"request_id": rid})
        assert api._requests[rid]["stage_events_cursor"] == 1
        api.publish_answer_event(rid, "菜单", "m", ["e"], event_id=f"ev_answer_{rid}")
        assert api._requests[rid]["stage_events_cursor"] == 2
        api.publish_result_committed(rid, {"plan_id": "p"}, event_id=f"ev_result_{rid}")
        assert api._requests[rid]["stage_events_cursor"] == 3
        # 重复 event_id → 不递增
        api.publish_answer_event(rid, "菜单", "m", ["e"], event_id=f"ev_answer_{rid}")
        assert api._requests[rid]["stage_events_cursor"] == 3
        # GET 状态返回精确整数值
        code, resp = api.get_request_status(rid)
        assert code == 200 and resp["stage_events_cursor"] == 3

    def test_get_status_projects_forbidden_fields(self) -> None:
        """GET 状态 result_summary 统一禁止字段投影，不泄漏 user_id/disease_name。"""
        rid = _unique("r")
        api._requests[rid] = {
            "request_id": rid, "status": "completed", "session_id": "s",
            "created_at": "t", "stage_events_cursor": 1,
            "result_summary": {"plan_id": "p", "user_id": 7,
                               "disease_name": "糖尿病"},
            "error": {"code": "X", "user_id": 7},
        }
        code, resp = api.get_request_status(rid)
        assert code == 200
        assert "user_id" not in resp["result_summary"]
        assert "disease_name" not in resp["result_summary"]
        assert resp["result_summary"]["plan_id"] == "p"
        assert "user_id" not in resp["error"]

    def test_blocked_answer_halts_success_chain(self) -> None:
        """answer_ready 含禁止键 → publish 抛 SensitiveDataBlocked（阻止成功链）。"""
        rid = _unique("r")
        api._requests[rid] = {"request_id": rid, "status": "running"}
        with pytest.raises(SensitiveDataBlocked):
            api.publish_answer_event(rid, "菜单", {"user_id": 7}, ["e"],
                                     event_id=f"ev_answer_{rid}")
        types = [e["event"] for e in api.subscribe_events(rid)]
        assert "answer_ready" not in types
        assert "error" in types  # 发出 error 事件
        assert "result_committed" not in types

    @pytest.mark.skipif(not _mysql_available(), reason="MySQL 不可用")
    def test_blocked_answer_halts_outbox_dispatch(self) -> None:
        """answer_ready 被拦截 → outbox dispatcher 停止：result_committed 不发布、outbox 不标记 dispatched。"""
        import pymysql

        from food_agent_v2.application.outbox import OutboxDispatcher
        from food_agent_v2.core.config import load_config
        rid = _unique("r")
        api._requests[rid] = {"request_id": rid, "status": "running", "session_id": "s"}
        cfg = load_config().mysql
        conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                               password=cfg.password, database=cfg.database,
                               charset="utf8mb4")
        cur = conn.cursor()
        cur.execute("DELETE FROM outbox WHERE request_id=%s", (rid,))
        cur.execute(
            "INSERT INTO outbox (event_id, request_id, event_type, payload, seq, status) "
            "VALUES (%s,%s,%s,%s,%s,'pending')",
            (f"ev_answer_{rid}", rid, "answer_ready",
             json.dumps({"text": "菜单", "menu_ref": {"user_id": 7},
                         "evidence_refs": []}), 1))
        cur.execute(
            "INSERT INTO outbox (event_id, request_id, event_type, payload, seq, status) "
            "VALUES (%s,%s,%s,%s,%s,'pending')",
            (f"ev_result_{rid}", rid, "result_committed",
             json.dumps({"menu_summary": {"plan_id": "p"}}), 2))
        conn.commit()
        conn.close()
        try:
            OutboxDispatcher(d1_api=api).dispatch_request(rid)
            types = [e["event"] for e in api.subscribe_events(rid)]
            assert "answer_ready" not in types
            assert "result_committed" not in types
            conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                                   password=cfg.password, database=cfg.database,
                                   charset="utf8mb4")
            cur = conn.cursor()
            cur.execute("SELECT status, COUNT(*) FROM outbox WHERE request_id=%s "
                        "GROUP BY status", (rid,))
            statuses = {r[0]: r[1] for r in cur.fetchall()}
            conn.close()
            assert statuses.get("dispatched", 0) == 0  # 未当作成功投递
        finally:
            conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                                   password=cfg.password, database=cfg.database,
                                   charset="utf8mb4")
            cur = conn.cursor()
            cur.execute("DELETE FROM outbox WHERE request_id=%s", (rid,))
            conn.commit()
            conn.close()

    def test_idempotency_survives_instance_restart(self) -> None:
        """新 RecommendationAPI 实例（模拟进程重启）仍识别幂等键：同载荷 200、不同载荷 409。"""
        from food_agent_v2.d1 import RecommendationAPI
        body = {
            "idempotency_key": _unique("ik"),
            "participants": [{"participant_ref": "p1", "user_id": 1}],
            "message": "推荐菜单",
            "config": {},
        }
        key = body["idempotency_key"]
        try:
            code1, resp1 = api.create_request(body)
            assert code1 == 202
            fresh = RecommendationAPI()
            code2, resp2 = fresh.create_request(dict(body))
            assert code2 == 200
            assert resp2["request_id"] == resp1["request_id"]
            diff = dict(body)
            diff["message"] = "不同消息"
            code3, resp3 = fresh.create_request(diff)
            assert code3 == 409
            assert resp3["error"] == "IDEMPOTENCY_KEY_REUSED"
            assert resp3["existing_request_id"] == resp1["request_id"]
        finally:
            _delete_redis_keys(f"v2:idem:{key}", f"v2:request:{resp1['request_id']}")

    def test_concurrent_idempotency_single_request_id(self) -> None:
        """并发两个实例同键同载荷 → 只有一个 202，其余 200 且同一 request_id。"""
        import threading

        from food_agent_v2.d1 import RecommendationAPI
        body = {
            "idempotency_key": _unique("ikc"),
            "participants": [{"participant_ref": "p1", "user_id": 1}],
            "message": "并发菜单",
            "config": {},
        }
        key = body["idempotency_key"]
        results: list[tuple[int, dict]] = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            results.append(RecommendationAPI().create_request(dict(body)))

        try:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
            codes = sorted(r[0] for r in results)
            assert codes == [200, 202]
            rid_set = {r[1]["request_id"] for r in results}
            assert len(rid_set) == 1  # 同一 request_id
        finally:
            rid = next(iter(rid_set)) if "rid_set" in dir() else None
            for k in (f"v2:idem:{key}", *(f"v2:request:{rid}" if rid else ())):
                _delete_redis_keys(k)

    @pytest.mark.skipif(not _mysql_available(), reason="MySQL 不可用")
    def test_session_get_returns_real_data_and_404(self, client) -> None:
        """session：不存在 404；创建（201）后返回真实 participant_refs/request_count/current_menu。"""
        resp = client.get(f"/v1/sessions/{_unique('missing')}")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "NOT_FOUND"

        resp = client.post("/v1/sessions",
                           json={"participants": [{"participant_ref": "p1"}]})
        assert resp.status_code == 201
        sid = resp.json()["session_id"]
        try:
            resp = client.get(f"/v1/sessions/{sid}")
            assert resp.status_code == 200
            body = resp.json()
            assert body["session_id"] == sid
            assert body["participant_refs"] == ["p1"]
            assert body["request_count"] == 0
            assert "current_menu" in body
            assert "last_request_at" in body
        finally:
            _delete_mysql_session(sid)

    def test_session_201_includes_last_request_at(self, client) -> None:
        """POST /v1/sessions 返回 201 且含约定的 last_request_at。"""
        resp = client.post("/v1/sessions",
                           json={"participants": [{"participant_ref": "p1"}]})
        assert resp.status_code == 201
        body = resp.json()
        assert "session_id" in body
        assert "last_request_at" in body
        _delete_mysql_session(body["session_id"])

    def test_session_infra_failure_is_503_not_404(self, client, monkeypatch) -> None:
        """session 存储基础设施异常 → 503，不得伪装成 404。"""
        import food_agent_v2.c4 as c4mod

        def boom(self, session_id):
            raise RuntimeError("mysql down")

        monkeypatch.setattr(c4mod.ContextService, "get_session_state", boom)
        resp = client.get(f"/v1/sessions/{_unique('x')}")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "SESSION_STORE_UNAVAILABLE"
        assert body["error"] != "NOT_FOUND"

    def test_session_route_no_direct_mysql_dependency(self) -> None:
        """api_app 不得直接依赖 pymysql/MySQLSessionMemorySource（只经 C4 公开接口）。"""
        import inspect

        import food_agent_v2.api_app as api_app_mod
        src = inspect.getsource(api_app_mod)
        assert "MySQLSessionMemorySource" not in src
        assert "import pymysql" not in src
        assert "ContextService" in src  # 经 C4 公开接口

    def test_duplicate_cancel_returns_409(self) -> None:
        """终态 cancelled 重复取消 → 409（cancelled 在终态集合）。"""
        rid = _unique("r")
        api._requests[rid] = {"request_id": rid, "status": "accepted", "session_id": "s"}
        code, _ = api.cancel_request(rid)
        assert code == 200
        code2, resp2 = api.cancel_request(rid)
        assert code2 == 409
        assert resp2["error"] == "REQUEST_ALREADY_TERMINAL"
        assert resp2["current_status"] == "cancelled"

    def test_non_object_body_returns_422(self, client) -> None:
        """两个 POST 接口对 null/数组/字符串等非对象请求体统一返回顶层 422。"""
        for content in ("null", "[]", "[1,2]", '"string"'):
            resp = client.post("/v1/recommendation-requests", content=content)
            assert resp.status_code == 422, content
            assert resp.json()["error"] == "VALIDATION_FAILED"
            assert "detail" not in resp.json()
        for content in ("null", "[]", '"string"'):
            resp = client.post("/v1/sessions", content=content)
            assert resp.status_code == 422, content
            assert resp.json()["error"] == "VALIDATION_FAILED"
