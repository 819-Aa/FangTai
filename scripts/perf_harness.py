"""P1 性能 harness —— 跑竞赛 20 组对话用例，输出逐用例端到端耗时。

通过 HTTP API 发请求（真实 LLM + SiliconFlow + 完整基础设施），测客户端侧
e2e（POST → 终态）。服务端节点级耗时由 API 进程的 ``[V2][perf]`` 结构化日志
输出，本脚本只负责客户端侧 e2e 与逐用例/逐轮结果。

用法：
    .venv/Scripts/python.exe scripts/perf_harness.py \
        [--api http://localhost:8003] \
        [--cases data/raw/对话用例.json] \
        [--max-wait 180]

前置：API 服务器与 MySQL/Qdrant/Redis/SiliconFlow/Qwen 就绪（同
tests/e2e/test_full_chain_real.py 的 H07 live 前置）。
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
import uuid

TERMINAL = {
    "completed", "failed", "no_safe_menu", "no_feasible_menu",
    "needs_clarification", "cancelled", "interrupted",
    "strict_time_indeterminate",
}


def _post(api: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{api}/v1/recommendation-requests", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get_status(api: str, request_id: str) -> dict:
    with urllib.request.urlopen(
            f"{api}/v1/recommendation-requests/{request_id}", timeout=30) as resp:
        return json.loads(resp.read())


def _wait_terminal(api: str, request_id: str, max_wait: int) -> dict:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        s = _get_status(api, request_id)
        if s["status"] in TERMINAL:
            return s
        time.sleep(1)
    return _get_status(api, request_id)


def load_cases(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def measure_turn(event_stream):
    """消费 SSE 事件流（(event_name, elapsed_ms) 序列），返回双 TTFT。

    visible_ttft_ms = 首个 answer_started；authoritative_ttft_ms = 首个 answer_ready。
    """
    visible_ttft_ms = None
    authoritative_ttft_ms = None
    for name, ts_ms in event_stream:
        if name == "answer_started" and visible_ttft_ms is None:
            visible_ttft_ms = ts_ms
        if name == "answer_ready" and authoritative_ttft_ms is None:
            authoritative_ttft_ms = ts_ms
    return {"visible_ttft_ms": visible_ttft_ms,
            "authoritative_ttft_ms": authoritative_ttft_ms}


def evaluate_turn(status, visible_ttft_ms, authoritative_ttft_ms, e2e_ms):
    """判定单轮性能是否通过；业务失败不计通过。"""
    return {
        "passed": status == "completed",
        "status": status,
        "visible_ttft_ms": visible_ttft_ms,
        "authoritative_ttft_ms": authoritative_ttft_ms,
        "e2e_ms": e2e_ms,
    }


def multi_turn_average(turn_times):
    """同一测试会话各轮 e2e 的算术平均。"""
    return sum(turn_times) / len(turn_times) if turn_times else 0.0


def participants_for(messages: list[str]) -> list[dict]:
    """从用例文本粗判参与人数（P1 基线；精确多人映射留到 P7 正式验收）。

    仅按显式人数词推断；无法判定时默认单人 p1。健康档案内部映射 pN → user_id N。
    相对称谓（小孩/老人）不在此推断——健康档案无角色标签，无法正确映射，
    交由 FastIntentRouter 澄清。
    """
    joined = " ".join(messages)
    if "六个人" in joined or "六人" in joined:
        return [{"participant_ref": f"p{i}"} for i in range(1, 7)]
    if "一家四口" in joined or "四个人" in joined or "四口" in joined:
        return [{"participant_ref": f"p{i}"} for i in range(1, 5)]
    if "两个人" in joined or "两人" in joined:
        return [{"participant_ref": "p1"}, {"participant_ref": "p2"}]
    return [{"participant_ref": "p1"}]


def run_turn(api: str, payload: dict, max_wait: int) -> dict:
    """POST 后订阅 SSE 记录双 TTFT，同时轮询终态（真实消费 SSE，非仅轮询）。"""
    import threading

    t0 = time.perf_counter()
    resp = _post(api, payload)
    rid = resp["request_id"]
    ttft = {"visible": None, "authoritative": None}

    def _consume_sse():
        req = urllib.request.Request(
            f"{api}/v1/recommendation-requests/{rid}/events",
            headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=max_wait) as stream:
                for raw in stream:
                    line = raw.decode(errors="ignore").strip()
                    if line.startswith("event:"):
                        name = line.split(":", 1)[1].strip()
                        ts_ms = round((time.perf_counter() - t0) * 1000, 1)
                        if name == "answer_started" and ttft["visible"] is None:
                            ttft["visible"] = ts_ms
                        elif name == "answer_ready" and ttft["authoritative"] is None:
                            ttft["authoritative"] = ts_ms
        except Exception:
            pass

    thread = threading.Thread(target=_consume_sse, daemon=True)
    thread.start()
    result = _wait_terminal(api, rid, max_wait)
    e2e_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "request_id": rid,
        "status": result.get("status"),
        "e2e_ms": e2e_ms,
        "visible_ttft_ms": ttft["visible"],
        "authoritative_ttft_ms": ttft["authoritative"],
        "session_id": result.get("session_id", ""),
    }


def run_case(api: str, case: dict, max_wait: int) -> dict:
    turns = []
    session_id = None
    participants = participants_for(case["user_messages"])
    for msg in case["user_messages"]:
        payload = {
            "idempotency_key": f"perf-{uuid.uuid4().hex[:12]}",
            "participants": participants,
            "message": msg,
            "config": {},
        }
        if session_id:
            payload["session_id"] = session_id
        turn = run_turn(api, payload, max_wait)
        session_id = turn.get("session_id") or session_id
        turns.append(turn)
    return {"case_id": case["id"], "turn_count": case["turn_count"], "turns": turns}


def main() -> int:
    parser = argparse.ArgumentParser(description="P1 性能 harness")
    parser.add_argument("--api", default=os.environ.get("PERF_API_BASE", "http://localhost:8003"))
    parser.add_argument("--cases", default="data/raw/对话用例.json")
    parser.add_argument("--max-wait", type=int, default=180)
    args = parser.parse_args()

    cases = load_cases(args.cases)
    print(f"harness: api={args.api} cases={len(cases)}")
    print(f"{'case':>4} {'turns':>5} {'status':>24} {'e2e_ms':>9}")

    all_e2e: list[float] = []
    for case in cases:
        r = run_case(args.api, case, args.max_wait)
        for t in r["turns"]:
            all_e2e.append(t["e2e_ms"])
            print(f"{r['case_id']:>4} {r['turn_count']:>5} {t['status']:>24} "
                  f"{t['e2e_ms']:>9.1f}")

    if all_e2e:
        n = len(all_e2e)
        avg = sum(all_e2e) / n
        all_e2e.sort()
        p50 = all_e2e[n // 2]
        p95 = all_e2e[min(n - 1, int(n * 0.95))]
        print(f"\n汇总: n={n} avg={avg:.1f}ms p50={p50:.1f}ms "
              f"p95={p95:.1f}ms max={all_e2e[-1]:.1f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
