import argparse
import json
import time
import uuid
from pathlib import Path

import httpx

from food_agent_v2.c2.planner import MenuPlanner
from food_agent_v2.c4 import ContextService
from food_agent_v2.d1.progress import NODE_TITLES, STATUS_SUFFIX
from verification.live_chain_audit import TERMINAL, sse

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://127.0.0.1:8003")
parser.add_argument("--minutes", type=int, default=180)
parser.add_argument(
    "--output", default="verification/artifacts/four-dishes-soup-after-2026-10-04.json"
)
a = parser.parse_args()
p = Path(a.output)
with httpx.Client(base_url=a.base, trust_env=False, timeout=20) as c:
    sid = c.post("/v1/sessions", json={"participants": [{"participant_ref": "p1"}]}).json()[
        "session_id"
    ]
    payload = {
        "session_id": sid,
        "participants": [{"participant_ref": "p1"}],
        "idempotency_key": "soup-audit-" + uuid.uuid4().hex,
        "message": f"今晚晚餐请安排四菜一汤，家常口味，{a.minutes}分钟内完成",
    }
    r = c.post("/v1/recommendation-requests", json=payload)
    r.raise_for_status()
    rid = r.json()["request_id"]
    print("request_id=" + rid, flush=True)
    for _ in range(300):
        state = c.get("/v1/recommendation-requests/" + rid).json()
        if state["status"] in TERMINAL:
            break
        time.sleep(1)
    for _ in range(35):
        events = sse(c, rid)
        if state["status"] != "completed" or any(e["event"] == "result_committed" for e in events):
            break
        time.sleep(1)
    menu = (state.get("result_summary") or {}).get("menu_summary") or {}
    kinds = [MenuPlanner._classify_dish_type(it["name"]) for it in menu.get("items", [])]
    current = c.get("/v1/sessions/" + sid).json().get("current_menu") or {}
    storage = ContextService()
    log = storage.load_recommendation_log(rid) or {}
    checks = {
        "completed": state["status"] == "completed",
        "exact_five": len(menu.get("items", [])) == 5,
        "one_soup": kinds.count("soup") == 1,
        "four_main_dishes": kinds.count("main") == 4,
        "mysql_status": log.get("status") == "completed",
        "menu_consistent": bool(menu)
        and menu.get("plan_id") == current.get("plan_id")
        and menu.get("menu_hash") == current.get("menu_hash"),
        "committed_event": any(
            e["event"] == "result_committed" and e["data"]["menu_summary"] == menu for e in events
        ),
        "fixed_templates": all(
            e["data"]["summary"]
            == NODE_TITLES[e["data"]["node_id"]] + STATUS_SUFFIX[e["data"]["status"]]
            for e in events
            if e["event"] == "thought_node"
        ),
    }
    report = {
        "mock": False,
        "request": payload,
        "state": state,
        "events": events,
        "dish_kinds": kinds,
        "checks": checks,
        "complete": all(checks.values()),
    }
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": state["status"],
                "items": menu.get("items", []),
                "kinds": kinds,
                "checks": checks,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    assert report["complete"]
