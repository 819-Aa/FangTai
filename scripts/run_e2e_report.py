"""端到端集成测试报告生成器。

跑 20 组竞赛对话用例（单人 + 多人），采集：用户档案映射（participant_ref →
user_id）、每轮消息、端到端耗时、双 TTFT（visible/authoritative）、SSE 事件
序列；合并服务端 PerfTrace（各节点耗时 + 模型调用），生成自包含 HTML 报告。

用法：
    .venv/Scripts/python.exe scripts/run_e2e_report.py \
        --api http://localhost:8001 \
        --cases data/raw/对话用例.json \
        --perf-log <API 进程日志文件路径> \
        --output reports/e2e_report.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from perf_harness import TERMINAL, load_cases, participants_for  # noqa: E402

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
        time.sleep(0.3)
    return _get_status(api, request_id)


def run_turn(api: str, payload: dict, max_wait: int) -> dict:
    """POST + 订阅 SSE 记录事件序列 + 双 TTFT，同时轮询终态。"""
    t0 = time.perf_counter()
    resp = _post(api, payload)
    rid = resp["request_id"]
    session_id = resp.get("session_id", "")
    events = []  # [(event_name, elapsed_ms), ...]

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
                        ts = round((time.perf_counter() - t0) * 1000, 1)
                        events.append((name, ts))
        except Exception:
            pass

    thread = threading.Thread(target=_consume_sse, daemon=True)
    thread.start()
    result = _wait_terminal(api, rid, max_wait)
    e2e_ms = round((time.perf_counter() - t0) * 1000, 1)

    visible = None
    authoritative = None
    for name, ts in events:
        if name == "answer_started" and visible is None:
            visible = ts
        elif name == "answer_ready" and authoritative is None:
            authoritative = ts

    return {
        "request_id": rid,
        "session_id": session_id,
        "status": result.get("status"),
        "e2e_ms": e2e_ms,
        "visible_ttft_ms": visible,
        "authoritative_ttft_ms": authoritative,
        "sse_events": events,
        "error": (result.get("error") or {}).get("code"),
    }


def parse_perf_log(log_path: str) -> dict[str, dict]:
    """从 API 进程日志解析 PerfTrace（[V2][perf] {...}），按 request_id 索引。"""
    perf_map = {}
    if not log_path or not os.path.exists(log_path):
        return perf_map
    with open(log_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "[V2][perf]" not in line:
                continue
            m = re.search(r"\[V2\]\[perf\] (\{.*\})", line)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            perf_map[data.get("request_id", "")] = data
    return perf_map


def _html_escape(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def generate_html(cases, turns, perf_map, output_path: str) -> None:
    total_turns = len(turns)
    completed = sum(1 for t in turns if t["status"] == "completed")
    e2e_ok = sum(1 for t in turns if t["e2e_ms"] is not None and t["e2e_ms"] < 15000)

    rows = []
    for t in turns:
        perf = perf_map.get(t["request_id"], {})
        nodes = perf.get("nodes_ms", {})
        node_cells = " ".join(
            f"{k}:{int(v)}ms" for k, v in nodes.items() if v is not None)
        sse = t.get("sse_events") or []
        sse_text = " → ".join(f"{n}@{int(ts)}ms" for n, ts in sse[:12])
        rows.append(f"""
        <tr class="{t['status']}">
          <td>{t['case_id']}</td>
          <td>{t['turn_index'] + 1}/{t['turn_count']}</td>
          <td>{_html_escape(t['participant_refs'])}</td>
          <td>{_html_escape(t['user_ids'])}</td>
          <td class="msg">{_html_escape(t['message'])}</td>
          <td>{_html_escape(t['status'])}</td>
          <td>{t['e2e_ms']}</td>
          <td>{t['visible_ttft_ms'] if t['visible_ttft_ms'] is not None else '—'}</td>
          <td>{t['authoritative_ttft_ms'] if t['authoritative_ttft_ms'] is not None else '—'}</td>
          <td>{perf.get('total_ms', '—')}</td>
          <td class="nodes">{_html_escape(node_cells)}</td>
          <td>{perf.get('model_call_count', '—')}</td>
          <td>{perf.get('model_total_ms', '—')}</td>
          <td class="sse">{_html_escape(sse_text)}</td>
        </tr>""")

    cases_html = []
    for case in cases:
        msgs = "<br>".join(_html_escape(m) for m in case["user_messages"])
        cases_html.append(f"""
        <tr>
          <td>{case['id']}</td>
          <td>{case['turn_count']}</td>
          <td class="msg">{msgs}</td>
        </tr>""")

    perf_gate = "✅ 通过" if (completed == total_turns and all(
        t["e2e_ms"] is not None and t["e2e_ms"] < 15000 for t in turns)) else "⚠️ 部分"

    html_doc = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>V2 端到端集成测试报告</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; color: #1a1a1a; }}
  h1 {{ border-bottom: 2px solid #333; padding-bottom: 8px; }}
  h2 {{ margin-top: 32px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
  th, td {{ border: 1px solid #ccc; padding: 4px 6px; text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; position: sticky; top: 0; }}
  .msg {{ max-width: 260px; word-break: break-all; }}
  .nodes, .sse {{ max-width: 300px; word-break: break-all; font-size: 11px; }}
  tr.completed {{ }}
  tr.strict_time_indeterminate, tr.needs_clarification {{ background: #fff8e1; }}
  tr.failed, tr.no_safe_menu, tr.no_feasible_menu {{ background: #ffebee; }}
  .summary {{ background: #f7f7f7; padding: 12px; border-radius: 6px; }}
  .kpi {{ display: inline-block; margin-right: 24px; }}
  .kpi b {{ font-size: 20px; }}
</style>
</head>
<body>
<h1>V2 个性化膳食推荐 —— 端到端集成测试报告</h1>
<p>生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')} | 模式：fast_path（确定性快速路径）</p>

<div class="summary">
  <span class="kpi">用例组 <b>{len(cases)}</b></span>
  <span class="kpi">总轮数 <b>{total_turns}</b></span>
  <span class="kpi">completed <b>{completed}</b></span>
  <span class="kpi">单轮 &lt;15s 达标 <b>{e2e_ok}/{total_turns}</b></span>
  <span class="kpi">性能门禁 <b>{perf_gate}</b></span>
</div>

<h2>一、对话用例总览</h2>
<table>
  <tr><th>用例 ID</th><th>轮数</th><th>消息</th></tr>
  {''.join(cases_html)}
</table>

<h2>二、逐轮执行明细</h2>
<table>
  <tr>
    <th>用例</th><th>轮次</th><th>参与者</th><th>用户档案 ID</th><th>消息</th>
    <th>终态</th><th>e2e(ms)</th><th>可见TTFT(ms)</th><th>权威TTFT(ms)</th>
    <th>服务端总耗时(ms)</th><th>节点耗时</th><th>模型调用数</th><th>模型耗时(ms)</th><th>SSE 事件序列</th>
  </tr>
  {''.join(rows)}
</table>

<p><em>注：参与者 pN 内部映射到用户档案 user_id=N（data/raw 50 份标准化档案）。</em></p>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_doc)


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端集成测试报告")
    parser.add_argument("--api", default=os.environ.get("PERF_API_BASE", "http://localhost:8001"))
    parser.add_argument("--cases", default="data/raw/对话用例.json")
    parser.add_argument("--perf-log", default="")
    parser.add_argument("--max-wait", type=int, default=120)
    parser.add_argument("--output", default="reports/e2e_report.html")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    turns = []
    for case in cases:
        participants = participants_for(case["user_messages"])
        session_id = None
        for i, msg in enumerate(case["user_messages"]):
            payload = {
                "idempotency_key": f"e2e-{uuid.uuid4().hex[:12]}",
                "participants": participants,
                "message": msg,
                "config": {},
            }
            if session_id:
                payload["session_id"] = session_id
            t = run_turn(args.api, payload, args.max_wait)
            t.update({
                "case_id": case["id"],
                "turn_index": i,
                "turn_count": case["turn_count"],
                "participant_refs": [p["participant_ref"] for p in participants],
                "user_ids": [int(p["participant_ref"][1:]) for p in participants],
                "message": msg,
            })
            session_id = t.get("session_id") or session_id
            turns.append(t)
            print(f"case {case['id']} turn {i + 1}: {t['status']} e2e={t['e2e_ms']}ms")

    perf_map = parse_perf_log(args.perf_log)
    print(f"perf traces: {len(perf_map)}")
    generate_html(cases, turns, perf_map, args.output)
    print(f"报告已生成: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
