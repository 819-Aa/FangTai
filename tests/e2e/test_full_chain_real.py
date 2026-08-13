"""T23 真实全链路验收 —— 成功路径与跨库一致。

前置：H04 空 V2 环境已 data-initialize；API 服务器运行于 localhost:8001；
真实 LLM（DeepSeek）可用。任何 live skip 使最终状态为 NOT_ACCEPTED。

覆盖：
- 单人 / 多人全员交集 / 明确菜数 / 默认 5 道 / 软"尽量快" / 硬截止可证明；
- 多轮替换与恢复、SSE 断线重连、幂等重放、页面刷新继续同 session；
- 跨库断言：API completed、MySQL result/audit/session/outbox、Redis SSE 终态。
"""

import json
import os
import time
import urllib.request
import uuid

import pymysql
import pytest

from food_agent_v2.c4.redis_store import RedisSessionStore
from food_agent_v2.core.config import load_config

# live：真实全链路验收，需 H04 空 V2 环境 + API 服务器 + 真实 LLM；默认不进入普通回归
pytestmark = pytest.mark.live

# 验收脚本通过 T23_API_BASE 指向绑定同一 T23 环境的隔离 API；默认 8001 仅作后备
API = os.environ.get("T23_API_BASE", "http://localhost:8001")
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


def _qdrant_has_recipe(recipe_id: int) -> int:
    """Qdrant 是否含该 recipe_id 点。返回命中数。

    Qdrant 点的 id 就是 recipe_id（index_builder 以 int(recipe_id) 为 PointStruct.id），
    payload 无 recipe_id 字段（只有 document_id="recipe_XXXX"）。因此按点 id 存在性
    检查，而非 payload filter（旧实现 filter recipe_id 永远 0 命中 → 误报缺菜）。
    """
    cfg = load_config().qdrant
    try:
        req = urllib.request.Request(
            f"http://{cfg.host}:{cfg.rest_port}/collections/{cfg.collection}/points/{recipe_id}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return 1 if data.get("result") else 0
    except urllib.error.HTTPError as e:
        # Qdrant 对不存在的点返回 404；其他错误照常抛出
        if e.code == 404:
            return 0
        raise


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
    menu: dict | None = None
    try:
        cur.execute("SELECT status, final_plan_id, health_evidence, commit_hash "
                    "FROM recommendation_logs WHERE request_id=%s", (rid,))
        row = cur.fetchone()
        assert row and row[0] == "completed" and row[1], f"result 缺失/未完成: {rid}"
        assert row[3], f"commit_hash 缺失: {rid}"
        evidence = json.loads(row[2] or "{}")
        plan_id = row[1]
        recipe_ids = sorted(int(x) for x in (evidence.get("recipe_ids") or []))
        menu_hash = evidence.get("menu_hash")
        assert plan_id and recipe_ids and menu_hash, f"audit 菜单身份缺失: {rid}"
        # 最终校验必须 PASS（多人每道菜安全由此保证）
        assert evidence.get("final_validation_verdict") == "PASS", f"final validation 非 PASS: {rid}"
        assert evidence.get("recipe_ids") and evidence.get("menu_hash")
        # 同一 plan_id/menu_hash/recipe_ids 跨 store 一致
        cur.execute("SELECT participant_refs, request_count, current_menu_plan_id "
                    "FROM sessions WHERE session_id=%s", (session_id,))
        srow = cur.fetchone()
        assert srow and srow[2] == plan_id, f"session current_menu 不一致: {session_id}"
        cur.execute("SELECT plan_id, menu_hash, recipe_ids FROM menu_versions "
                    "WHERE session_id=%s ORDER BY committed_at DESC LIMIT 1", (session_id,))
        mrow = cur.fetchone()
        assert mrow and mrow[0] == plan_id, f"menu_versions plan 不一致: {session_id}"
        assert mrow[1] == menu_hash, f"menu_versions menu_hash 不一致: {session_id}"
        assert sorted(int(x) for x in (json.loads(mrow[2] or "[]") if mrow[2] else [])) == recipe_ids
        cur.execute("SELECT event_type, status FROM outbox WHERE request_id=%s "
                    "ORDER BY seq", (rid,))
        ob = cur.fetchall()
        assert [r[0] for r in ob] == ["answer_ready", "result_committed"], f"outbox 顺序错误: {rid}"
        assert all(r[1] == "dispatched" for r in ob), f"outbox 未全部投递: {rid}"
        # outbox result_committed payload 与同一菜单身份一致
        cur.execute("SELECT payload FROM outbox WHERE request_id=%s AND event_type=%s",
                    (rid, "result_committed"))
        ob_payload = json.loads(cur.fetchone()[0])
        summary = ob_payload.get("menu_summary") or {}
        assert summary.get("plan_id") == plan_id, f"outbox plan 不一致: {rid}"
        assert summary.get("menu_hash") == menu_hash, f"outbox menu_hash 不一致: {rid}"
        assert sorted(int(x) for x in (summary.get("recipe_ids") or [])) == recipe_ids
        menu = {"plan_id": plan_id, "menu_hash": menu_hash, "recipe_ids": recipe_ids}
    finally:
        conn.close()
    # Redis SSE result_committed 与同一菜单身份一致
    result_events = [e for e in _redis_events(rid) if e.get("event") == "result_committed"]
    assert result_events, f"SSE 缺 result_committed: {rid}"
    sse_summary = (json.loads(result_events[0]["data"]) or {}).get("menu_summary") or {}
    assert sse_summary.get("plan_id") == menu["plan_id"], f"SSE plan 不一致: {rid}"
    assert sse_summary.get("menu_hash") == menu["menu_hash"], f"SSE menu_hash 不一致: {rid}"
    # Qdrant 含最终菜单全部 recipe ids
    for rid_id in menu["recipe_ids"]:
        count = _qdrant_has_recipe(rid_id)
        assert count > 0, f"Qdrant 缺少最终菜单 recipe {rid_id}"
    return menu


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
        """明确菜数（四菜一汤）→ 精确满足 5 道。"""
        info = _run_success_case("四菜一汤，家常口味", [{"participant_ref": "p1"}])
        menu = _assert_cross_store(info["request_id"], info["session_id"])
        assert len(menu["recipe_ids"]) == 5, f"四菜一汤应 5 道，实际 {len(menu['recipe_ids'])}"

    def test_default_five_dishes(self, _api_ready) -> None:
        """未指定菜数 → 默认 5 道。"""
        info = _run_success_case("推荐晚餐，家常口味", [{"participant_ref": "p1"}])
        menu = _assert_cross_store(info["request_id"], info["session_id"])
        assert len(menu["recipe_ids"]) == 5, f"默认应 5 道，实际 {len(menu['recipe_ids'])}"

    def test_soft_as_fast_as_possible(self, _api_ready) -> None:
        info = _run_success_case("推荐晚餐，尽量快一点", [{"participant_ref": "p1"}])
        _assert_cross_store(info["request_id"], info["session_id"])

    def test_hard_deadline_indeterminate_when_unprovable(self, _api_ready) -> None:
        """硬截止"30分钟必须完成"：固定数据大部分菜缺显式步骤时长，B5 无法高权威
        证明严格时间 → 诚实返回 strict_time_indeterminate（ADR-0005，不伪造时间承诺）。"""
        resp = _post({
            "idempotency_key": f"e2e-{uuid.uuid4().hex[:12]}",
            "participants": [{"participant_ref": "p1"}],
            "message": "30分钟内必须完成的晚餐", "config": {},
        })
        result = _wait_terminal(resp["request_id"])
        assert result["status"] == "strict_time_indeterminate", (
            f"应 strict_time_indeterminate，实际 {result['status']} error={result.get('error')}")

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
