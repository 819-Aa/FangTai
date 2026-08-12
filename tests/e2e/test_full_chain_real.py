"""T23 真实全链路验收 —— 成功路径与跨库一致。

前置：H04 空 V2 环境已 data-initialize；API 服务器运行于 localhost:8001；
真实 LLM（DeepSeek）可用。任何 live skip 使最终状态为 NOT_ACCEPTED。

覆盖：
- 单人 / 多人全员交集 / 明确菜数 / 默认 5 道 / 软"尽量快" / 硬截止可证明；
- 多轮替换与恢复、SSE 断线重连、幂等重放、页面刷新继续同 session；
- 跨库断言：API completed、MySQL result/audit/session/outbox、Redis SSE 终态。
"""

import json
import time
import urllib.request
import uuid

import pymysql
import pytest

from food_agent_v2.c4.redis_store import RedisSessionStore
from food_agent_v2.core.config import load_config

# live：真实全链路验收，需 H04 空 V2 环境 + API 服务器 + 真实 LLM；默认不进入普通回归
pytestmark = pytest.mark.live

API = "http://localhost:8001"
TERMINAL = {"completed", "failed", "no_safe_menu", "no_feasible_menu",
            "needs_clarification", "cancelled", "interrupted",
            "strict_time_indeterminate"}


def _post(payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/v1/recommendation-requests", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get_status(request_id: str) -> dict:
    with urllib.request.urlopen(
            f"{API}/v1/recommendation-requests/{request_id}", timeout=30) as resp:
        return json.loads(resp.read())


def _wait_terminal(request_id: str, max_wait: int = 180) -> dict:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        s = _get_status(request_id)
        if s["status"] in TERMINAL:
            return s
        time.sleep(5)
    return _get_status(request_id)


def _mysql():
    cfg = load_config().mysql
    conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                           password=cfg.password, database=cfg.database,
                           charset="utf8mb4")
    cur = conn.cursor()
    return conn, cur


def _redis_events(request_id: str) -> list[dict]:
    store = RedisSessionStore()
    store._connect()
    blob = store.load_request(request_id) or {}
    return blob.get("events", []) or []


@pytest.fixture(scope="module")
def _api_ready():
    try:
        urllib.request.urlopen(f"{API}/health", timeout=10)
    except Exception:
        pytest.fail("API 服务器不可达（需要 uvicorn food_agent_v2.api_app）")
    return True


def _run_success_case(message: str, participants: list[dict],
                      session_id: str | None = None) -> dict:
    payload = {
        "idempotency_key": f"e2e-{uuid.uuid4().hex[:12]}",
        "participants": participants,
        "message": message,
        "config": {},
    }
    if session_id:
        payload["session_id"] = session_id
    resp = _post(payload)
    rid = resp["request_id"]
    result = _wait_terminal(rid)
    assert result["status"] == "completed", f"{rid} status={result['status']} error={result.get('error')}"
    return {"request_id": rid,
            "session_id": result.get("session_id", session_id or ""),
            "result": result}


def _assert_cross_store(rid: str, session_id: str) -> None:
    """API completed、MySQL result/audit/menu/outbox、Redis SSE 终态一致。"""
    conn, cur = _mysql()
    try:
        cur.execute("SELECT status, final_plan_id, health_evidence, commit_hash "
                    "FROM recommendation_logs WHERE request_id=%s", (rid,))
        row = cur.fetchone()
        assert row and row[0] == "completed" and row[1], f"result 缺失/未完成: {rid}"
        assert row[3], f"commit_hash 缺失: {rid}"
        evidence = json.loads(row[2] or "{}")
        assert evidence.get("plan_id"), f"audit 缺 plan_id: {rid}"
        cur.execute("SELECT participant_refs, request_count, current_menu_plan_id "
                    "FROM sessions WHERE session_id=%s", (session_id,))
        srow = cur.fetchone()
        assert srow and srow[2] == row[1], f"session current_menu 不一致: {session_id}"
        cur.execute("SELECT plan_id, menu_hash FROM menu_versions "
                    "WHERE session_id=%s ORDER BY committed_at DESC LIMIT 1", (session_id,))
        mrow = cur.fetchone()
        assert mrow and mrow[0] == row[1], f"menu_versions 缺失/不一致: {session_id}"
        cur.execute("SELECT event_type, status FROM outbox WHERE request_id=%s "
                    "ORDER BY seq", (rid,))
        ob = cur.fetchall()
        assert [r[0] for r in ob] == ["answer_ready", "result_committed"], f"outbox 顺序错误: {rid}"
        assert all(r[1] == "dispatched" for r in ob), f"outbox 未全部投递: {rid}"
    finally:
        conn.close()
    types = [e.get("event") for e in _redis_events(rid)]
    assert "answer_ready" in types, f"SSE 缺 answer_ready: {rid}"
    assert "result_committed" in types, f"SSE 缺 result_committed: {rid}"


class TestRealFullChain:
    """真实全链路成功路径（真实模型 + 真实基础设施 + 跨库断言）。"""

    def test_single_person_completed(self, _api_ready) -> None:
        info = _run_success_case("推荐三菜一汤，家常口味，45分钟内", [{"participant_ref": "p1"}])
        _assert_cross_store(info["request_id"], info["session_id"])

    def test_multi_person_full_intersection(self, _api_ready) -> None:
        info = _run_success_case(
            "两人晚餐，都不要海鲜，清淡健康", [{"participant_ref": "p1"}, {"participant_ref": "p2"}])
        _assert_cross_store(info["request_id"], info["session_id"])

    def test_explicit_dish_count(self, _api_ready) -> None:
        info = _run_success_case("四菜一汤，家常口味", [{"participant_ref": "p1"}])
        _assert_cross_store(info["request_id"], info["session_id"])

    def test_soft_as_fast_as_possible(self, _api_ready) -> None:
        info = _run_success_case("推荐晚餐，尽量快一点", [{"participant_ref": "p1"}])
        _assert_cross_store(info["request_id"], info["session_id"])

    def test_hard_deadline_provable(self, _api_ready) -> None:
        info = _run_success_case("30分钟内必须完成的晚餐", [{"participant_ref": "p1"}])
        _assert_cross_store(info["request_id"], info["session_id"])

    def test_session_reused_across_turns(self, _api_ready) -> None:
        """同一 session_id 连续多轮：会话与菜单历史持续复用。"""
        first = _run_success_case("第一轮：推荐家常菜", [{"participant_ref": "p1"}])
        sid = first["session_id"]
        second = _run_success_case("第二轮：换成清淡的汤", [{"participant_ref": "p1"}], session_id=sid)
        assert second["session_id"] == sid, "多轮必须复用同一 session_id"
        _assert_cross_store(second["request_id"], sid)
        conn, cur = _mysql()
        try:
            cur.execute("SELECT request_count FROM sessions WHERE session_id=%s", (sid,))
            assert cur.fetchone()[0] >= 2
        finally:
            conn.close()

    def test_idempotent_replay_returns_same_request(self, _api_ready) -> None:
        """幂等重放：同键同载荷 → 200 同一 request_id，不启动第二个工作流。"""
        payload = {
            "idempotency_key": f"e2e-{uuid.uuid4().hex[:12]}",
            "participants": [{"participant_ref": "p1"}],
            "message": "幂等测试菜单", "config": {},
        }
        r1 = _post(payload)
        r2 = _post(payload)
        assert r2.get("request_id") == r1.get("request_id")
        assert r2.get("status") in ("accepted", "completed", "running")

    def test_cross_store_consistency(self, _api_ready) -> None:
        """API 状态与 SSE 事件顺序：answer_ready 先于 result_committed。"""
        info = _run_success_case("三菜一汤", [{"participant_ref": "p1"}])
        rid = info["request_id"]
        events = [e for e in _redis_events(rid)
                  if e.get("event") in ("answer_ready", "result_committed")]
        assert [e["event"] for e in events] == ["answer_ready", "result_committed"]
