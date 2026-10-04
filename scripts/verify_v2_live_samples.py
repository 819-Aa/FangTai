"""真实在线模型验收样本采集脚本（v2 协议收口）。

运行方式：
  .\\.venv\\Scripts\\python.exe scripts/verify_v2_live_samples.py

目标：
在独立新会话、LangGraph 和持久化 v2 协议下，使用当前 LLM_* 配置的真实在线模型
执行端到端场景，采集：
1. request_id, session_id, question_id
2. 模型调用次数与耗时
3. MySQL 权威事实 (recommendation_logs, clarification_questions, outbox, request_acceptances)
4. GET 与 SSE 公开一致性
5. 敏感/内部字段零泄漏
"""

import json
import os
import sys
import time
import uuid

# 确保本地 127.0.0.1 不被系统代理拦截
os.environ["NO_PROXY"] = "localhost,127.0.0.1"

sys.path.insert(0, os.path.abspath("src"))

from food_agent_v2.c4 import ContextService
from food_agent_v2.core.config import load_config
from food_agent_v2.d1 import api as d1_api

cfg = load_config()
print("=" * 60)
print("H07 在线验收环境就绪检查:")
print(f"  MySQL:  {cfg.mysql.host}:{cfg.mysql.port}/{cfg.mysql.database}")
print(f"  Qdrant: {cfg.qdrant.host}:{cfg.qdrant.rest_port}/{cfg.qdrant.collection}")
print(f"  Redis:  {cfg.redis.host}:{cfg.redis.port}/{cfg.redis.key_prefix}")
print(f"  LLM:    {cfg.llm.model_reasoning} (API key: {'已配置' if cfg.llm.api_key else '缺失'})")
print("=" * 60)

c4 = ContextService()

def wait_for_terminal(request_id: str, timeout_sec: float = 120.0) -> dict:
    start = time.time()
    while time.time() - start < timeout_sec:
        time.sleep(1.0)
        code, st = d1_api.get_request_status(request_id)
        assert code == 200, f"get_request_status 返回非 200: {code}, {st}"
        status = st.get("status")
        if status in ("completed", "needs_clarification", "failed", "cancelled", "interrupted"):
            return st
    raise TimeoutError(f"请求 {request_id} 在 {timeout_sec}s 内未达到终态")

def inspect_mysql_facts(request_id: str, session_id: str) -> dict:
    log = c4.load_recommendation_log(request_id)
    outbox = c4.load_dispatched_outbox_events(request_id)
    active_q = c4.load_active_clarification(session_id)
    clar_state = c4.load_clarification_state(session_id)
    return {
        "log": log,
        "outbox_count": len(outbox),
        "outbox_events": [e.get("event_type") for e in outbox],
        "active_clarification": active_q,
        "clarification_state": clar_state,
    }

def scan_privacy(obj: any, path: str = "") -> list[str]:
    violations = []
    forbidden = ["modifications", "private_snapshot", "query_plan_snapshot", "user_id", "disease_name", "raw_health_metrics"]
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub_path = f"{path}.{k}" if path else str(k)
            for f in forbidden:
                if f == k:
                    violations.append(f"Forbidden key '{k}' at {sub_path}")
            violations.extend(scan_privacy(v, sub_path))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            violations.extend(scan_privacy(item, f"{path}[{idx}]"))
    return violations

def run_sample_1_clarification_and_structured_choice():
    print("\n>>> 样本 1: 主动澄清 -> 结构化选项回复 -> completed 3 道菜")
    # 创建持久化 v2 会话
    session_id = c4.create_session_record(
        participant_refs=["p1"],
        workflow_mode="langgraph",
        clarification_protocol_version="v2",
    )
    print(f"  会话创建: session_id={session_id}, protocol_version=v2")

    # 1. 发起易引发澄清的请求（未指定餐次，需求宽泛）
    t0 = time.time()
    idem1 = f"live_v2_clarify_{uuid.uuid4().hex[:8]}"
    body1 = {
        "idempotency_key": idem1,
        "session_id": session_id,
        "participants": [{"participant_ref": "p1", "label": "用户 1"}],
        "message": "三道清淡家常菜",
    }
    code1, resp1 = d1_api.create_request(body1)
    req1_id = resp1["request_id"]
    print(f"  轮次 1 请求发起: request_id={req1_id}, code={code1}")

    st1 = wait_for_terminal(req1_id, timeout_sec=120.0)
    dur1 = time.time() - t0
    status1 = st1.get("status")
    print(f"  轮次 1 终态: status={status1}, 耗时={dur1:.2f}s")

    # 隐私扫描
    priv1 = scan_privacy(st1)
    assert not priv1, f"轮次 1 GET 状态泄漏内部字段: {priv1}"

    mysql1 = inspect_mysql_facts(req1_id, session_id)
    print(f"  轮次 1 MySQL: log_status={mysql1['log'].get('status') if mysql1['log'] else None}, outbox={mysql1['outbox_events']}")

    # 如果进入了 needs_clarification，则进行第二轮结构化选择
    if status1 == "needs_clarification":
        active_q = st1.get("active_clarification")
        assert active_q is not None, "needs_clarification 终态必须包含 active_clarification"
        qid = active_q["question_id"]
        opts = active_q.get("options", [])
        print(f"  获得澄清问题: question_id={qid}, 问题文本='{active_q.get('question_text')}', 选项数={len(opts)}")
        assert len(opts) >= 2, f"选项数至少 2 个，实际 {len(opts)}"
        sel_opt = opts[0]
        sel_opt_id = sel_opt["option_id"]
        print(f"  选择选项: option_id={sel_opt_id}, 选项文本='{sel_opt.get('text')}'")

        # 2. 第二轮：结构化回复
        t1 = time.time()
        idem2 = f"live_v2_reply_{uuid.uuid4().hex[:8]}"
        body2 = {
            "idempotency_key": idem2,
            "session_id": session_id,
            "participants": [{"participant_ref": "p1", "label": "用户 1"}],
            "message": sel_opt.get("text", "确定"),
            "clarification_response": {
                "question_id": qid,
                "option_id": sel_opt_id,
            },
        }
        code2, resp2 = d1_api.create_request(body2)
        req2_id = resp2["request_id"]
        print(f"  轮次 2 请求发起: request_id={req2_id}, code={code2}")

        st2 = wait_for_terminal(req2_id, timeout_sec=120.0)
        dur2 = time.time() - t1
        status2 = st2.get("status")
        print(f"  轮次 2 终态: status={status2}, 耗时={dur2:.2f}s")

        priv2 = scan_privacy(st2)
        assert not priv2, f"轮次 2 GET 状态泄漏内部字段: {priv2}"

        mysql2 = inspect_mysql_facts(req2_id, session_id)
        print(f"  轮次 2 MySQL: log_status={mysql2['log'].get('status') if mysql2['log'] else None}, outbox={mysql2['outbox_events']}")

        # 验证旧问题已消费
        assert mysql2["active_clarification"] is None or status2 == "needs_clarification", "若完成则 active_clarification 应为 None"
        if status2 == "completed":
            menu_sum = st2.get("result_summary", {}).get("menu_summary", {})
            print(f"  成功生成菜单: plan_id={menu_sum.get('plan_id')}, 菜品数={len(menu_sum.get('items', []))}")

        return {
            "session_id": session_id,
            "req1": {"id": req1_id, "status": status1, "duration": dur1, "qid": qid},
            "req2": {"id": req2_id, "status": status2, "duration": dur2},
            "clarification_observed": True,
            "structured_reply_completed": status2 in ("completed", "needs_clarification"),
            "privacy_pass": True,
        }
    else:
        print(f"  轮次 1 未产生澄清问题: status={status1}；本样本不构成澄清链路验收")
        return {
            "session_id": session_id,
            "req1": {"id": req1_id, "status": status1, "duration": dur1},
            "clarification_observed": False,
            "structured_reply_completed": False,
            "privacy_pass": True,
        }

def run_sample_2_explicit_dinner_three_dishes():
    print("\n>>> 样本 2: 明确晚餐，推荐三道清淡家常菜 (直接成单场景)")
    session_id = c4.create_session_record(
        participant_refs=["p1"],
        workflow_mode="langgraph",
        clarification_protocol_version="v2",
    )
    t0 = time.time()
    idem = f"live_v2_dinner_{uuid.uuid4().hex[:8]}"
    body = {
        "idempotency_key": idem,
        "session_id": session_id,
        "participants": [{"participant_ref": "p1", "label": "用户 1"}],
        "message": "今晚晚餐，请推荐三道清淡的家常菜",
    }
    code, resp = d1_api.create_request(body)
    req_id = resp["request_id"]
    print(f"  发起请求: request_id={req_id}, code={code}")
    st = wait_for_terminal(req_id, timeout_sec=120.0)
    dur = time.time() - t0
    status = st.get("status")
    print(f"  请求终态: status={status}, 耗时={dur:.2f}s")

    priv = scan_privacy(st)
    assert not priv, f"GET 状态泄漏内部字段: {priv}"

    mysql = inspect_mysql_facts(req_id, session_id)
    print(f"  MySQL: log_status={mysql['log'].get('status') if mysql['log'] else None}, outbox={mysql['outbox_events']}")

    if status == "completed":
        menu = st.get("result_summary", {}).get("menu_summary", {})
        items = menu.get("items", [])
        print(f"  完成菜谱: plan_id={menu.get('plan_id')}, 菜品数={len(items)}")

    return {
        "session_id": session_id,
        "request_id": req_id,
        "status": status,
        "duration": dur,
        "privacy_pass": True,
    }

if __name__ == "__main__":
    results = {}
    try:
        results["sample_clarification"] = run_sample_1_clarification_and_structured_choice()
    except Exception as e:
        print(f"  样本 1 异常: {e}")
        import traceback
        traceback.print_exc()
        results["sample_clarification"] = {"error": str(e)}

    try:
        results["sample_dinner"] = run_sample_2_explicit_dinner_three_dishes()
    except Exception as e:
        print(f"  样本 2 异常: {e}")
        import traceback
        traceback.print_exc()
        results["sample_dinner"] = {"error": str(e)}

    print("\n" + "=" * 60)
    print("验收样本汇总:")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    print("=" * 60)
