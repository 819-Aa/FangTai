"""Read-only snapshots across an API restart; request IDs and files are explicit."""
import argparse
import json
from pathlib import Path

import httpx

from food_agent_v2.c4 import ContextService
from verification.live_chain_audit import sse

parser = argparse.ArgumentParser()
parser.add_argument("phase", choices=["before", "after"])
parser.add_argument("--request-id", action="append", default=[])
parser.add_argument("--baseline", type=Path, required=True)
parser.add_argument("--output", type=Path)
parser.add_argument("--base", default="http://127.0.0.1:8003")
a = parser.parse_args()
if a.phase == "before" and not a.request_id:
    parser.error("before requires --request-id")
if a.phase == "after" and not a.output:
    parser.error("after requires --output")
c4 = ContextService()
with httpx.Client(base_url=a.base, trust_env=False, timeout=10) as client:
    if a.phase == "before":
        records = []
        for rid in a.request_id:
            response = client.get(f"/v1/recommendation-requests/{rid}")
            response.raise_for_status()
            state = response.json()
            events = sse(client, rid)
            assert state["status"] != "running" and events
            records.append({"request_id": rid, "state": state, "events": events,
                            "generation": c4.load_request_acceptance_by_request_id(rid)["execution_generation"]})
        a.baseline.parent.mkdir(parents=True, exist_ok=True)
        a.baseline.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved restart baseline for", len(records), "requests")
    else:
        checks = []
        for record in json.loads(a.baseline.read_text(encoding="utf-8")):
            rid = record["request_id"]
            # First access MUST be SSE: a prior GET would mask cold identity restoration bugs.
            events = sse(client, rid)
            response = client.get(f"/v1/recommendation-requests/{rid}")
            response.raise_for_status()
            state = response.json()
            offset = len(record["events"]) // 2
            suffix = sse(client, rid, record["events"][offset]["id"])
            acceptance = c4.load_request_acceptance_by_request_id(rid)
            session = client.get(f"/v1/sessions/{state['session_id']}")
            session.raise_for_status()
            menu = (state.get("result_summary") or {}).get("menu_summary")
            current = session.json().get("current_menu") or {}
            same_menu = not menu or all(menu[k] == current[k] for k in ("plan_id", "menu_hash", "build_id", "recipe_ids", "items"))
            checks.append({"request_id": rid,
                           "http_ok": response.status_code == 200,
                           "status_unchanged": state["status"] == record["state"]["status"],
                           "result_unchanged": state.get("result_summary") == record["state"].get("result_summary"),
                           "cold_sse_events_unchanged": bool(events) and events == record["events"],
                           "last_event_id_exact_suffix": suffix == record["events"][offset + 1:],
                           "unique_ids": len(events) == len({e["id"] for e in events}),
                           "session_menu_matches": same_menu,
                           "no_reexecution": acceptance["execution_generation"] == record["generation"]})
        report = {"checks": checks, "passed": sum(sum(bool(v) for k,v in x.items() if k != "request_id") for x in checks),
                  "complete": bool(checks) and all(all(v for k, v in x.items() if k != "request_id") for x in checks)}
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))
        assert report["complete"]
