"""Real HTTP/model/storage probes. Creates isolated test sessions; no fixed data changes."""
from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from food_agent_v2.c4 import ContextService
from food_agent_v2.d1.schemas import scan_forbidden_fields

BASE = "http://127.0.0.1:8003"
OUTPUT = Path(os.environ.get("LIVE_AUDIT_OUTPUT", str(Path(__file__).parent / "artifacts" / "live-chain-2026-10-03.json")))
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "needs_clarification",
            "no_safe_menu", "no_feasible_menu", "strict_time_indeterminate", "recovery_required"}


def sse(client: httpx.Client, request_id: str, cursor: str | None = None) -> list[dict]:
    frames = []
    frame = {}
    try:
        with client.stream("GET", f"/v1/recommendation-requests/{request_id}/events",
                           headers={"Last-Event-ID": cursor} if cursor else {},
                           timeout=httpx.Timeout(10, read=2)) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    if frame.get("event"):
                        frames.append(frame)
                    frame = {}
                elif line.startswith("id: "):
                    frame["id"] = line[4:]
                elif line.startswith("event: "):
                    frame["event"] = line[7:]
                elif line.startswith("data: "):
                    frame["data"] = json.loads(line[6:])
    except httpx.ReadTimeout:
        # The production SSE stream stays open after terminal. Snapshot received above.
        pass
    return frames


def main() -> int:
    report = {"date": os.environ.get("LIVE_AUDIT_DATE", "2026-10-03"), "transport": "real HTTP", "mock": False,
              "scenarios": [], "blocked": [], "checks": []}
    storage = ContextService()
    with httpx.Client(base_url=BASE, trust_env=False, timeout=20) as client:
        ready = client.get("/ready")
        report["readiness"] = {"http_status": ready.status_code, "body": ready.json()}
        ready.raise_for_status()

        def create(message: str) -> tuple[dict, dict]:
            session = client.post("/v1/sessions", json={"participants": [{"participant_ref": "p1"}]})
            session.raise_for_status()
            payload = {"session_id": session.json()["session_id"], "participants": [{"participant_ref": "p1"}],
                       "idempotency_key": "live-audit-" + uuid.uuid4().hex, "message": message}
            response = client.post("/v1/recommendation-requests", json=payload)
            response.raise_for_status()
            return payload, response.json()

        def terminal(request_id: str) -> dict:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                state = client.get(f"/v1/recommendation-requests/{request_id}")
                state.raise_for_status()
                if state.json().get("status") in TERMINAL:
                    return state.json()
                time.sleep(1)
            raise TimeoutError("request did not reach terminal within 300 seconds")

        def check(name: str, passed: bool, details: dict | None = None) -> None:
            record = {"name": name, "passed": bool(passed), **(details or {})}
            report["checks"].append(record)
            print(json.dumps(record, ensure_ascii=True), flush=True)
            OUTPUT.parent.mkdir(exist_ok=True)
            OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        def facts(name: str, payload: dict, state: dict) -> dict:
            rid = state["request_id"]
            # GET can observe the committed transaction before the asynchronous
            # Outbox worker publishes its final event (30s recovery scan).
            dispatch_wait = time.monotonic()
            if state["status"] == "completed":
                deadline = dispatch_wait + 40
                while time.monotonic() < deadline:
                    dispatched = storage.load_dispatched_outbox_events(rid)
                    if any(e.get("event_type") == "result_committed" for e in dispatched):
                        break
                    time.sleep(0.5)
            events = sse(client, rid)
            cursor = events[0]["id"] if events else None
            resumed = sse(client, rid, cursor) if cursor else []
            log = storage.load_recommendation_log(rid)
            outbox = storage.load_dispatched_outbox_events(rid)
            record = {"name": name, "request_id": rid, "session_id": payload["session_id"],
                      "status": state["status"], "error_code": (state.get("error") or {}).get("code"),
                      "outbox_wait_seconds": round(time.monotonic() - dispatch_wait, 2),
                      "mysql_log_status": log.get("status") if log else None,
                      "outbox_types": [entry.get("event_type") for entry in outbox],
                      "sse_types": [event["event"] for event in events],
                      "sse_ids_unique": len({event["id"] for event in events}) == len(events),
                      "resume_exact_suffix": [event["id"] for event in resumed] == [event["id"] for event in events[1:]],
                      "public_forbidden_field_count": len(scan_forbidden_fields(state)) + sum(
                          len(scan_forbidden_fields(event["data"])) for event in events)}
            report["scenarios"].append(record)
            print(json.dumps(record, ensure_ascii=True), flush=True)
            check(name + "_sse_suffix", bool(events) and record["resume_exact_suffix"] and record["sse_ids_unique"])
            check(name + "_public_payload", record["public_forbidden_field_count"] == 0)
            check(name + "_mysql_terminal", record["mysql_log_status"] == state["status"],
                  {"http_status": state["status"], "mysql_status": record["mysql_log_status"]})
            if state["status"] == "completed":
                menu = (state.get("result_summary") or {}).get("menu_summary") or {}
                session = client.get(f"/v1/sessions/{payload['session_id']}").json()
                current = session.get("current_menu") or {}
                check(name + "_committed_menu", bool(menu.get("items")) and
                      menu.get("menu_hash") == current.get("menu_hash") and
                      menu.get("plan_id") == current.get("plan_id") and
                      "result_committed" in record["sse_types"] and
                      "result_committed" in record["outbox_types"])
            return record

        payload, response = create("今晚晚餐，请推荐三道清淡家常菜，45分钟内完成")
        state = terminal(response["request_id"])
        facts("recommendation", payload, state)
        check("recommendation_completed", state["status"] == "completed")
        repeated = client.post("/v1/recommendation-requests", json=payload)
        report["idempotent_replay"] = {"http_status": repeated.status_code,
            "same_request_id": repeated.json().get("request_id") == state["request_id"]}
        changed = client.post("/v1/recommendation-requests", json={**payload, "message": "different demand"})
        report["idempotent_conflict"] = {"http_status": changed.status_code, "error": changed.json().get("error")}
        check("idempotent_replay", repeated.status_code == 200 and report["idempotent_replay"]["same_request_id"])
        check("idempotent_conflict", changed.status_code == 409 and changed.json().get("error") == "IDEMPOTENCY_KEY_REUSED")

        menu = (state.get("result_summary") or {}).get("menu_summary")
        if not menu or state["status"] != "completed":
            report["blocked"].append("Real replacement/concurrent replacement/commit rollback require a committed menu; recommendation did not complete")
        else:
            def replacement(source: dict, target: int) -> dict:
                return {"session_id": payload["session_id"], "participants": payload["participants"],
                        "idempotency_key": "live-replace-" + uuid.uuid4().hex,
                        "message": "只替换这道菜，保持其他菜品不变，清淡家常口味",
                        "action": "replace_dish", "target_recipe_id": target,
                        "source_plan_id": source["plan_id"], "source_menu_hash": source["menu_hash"]}

            initial_menu = menu
            target = int(initial_menu["recipe_ids"][0])
            replace_payload = replacement(initial_menu, target)
            accepted = client.post("/v1/recommendation-requests", json=replace_payload)
            check("replacement_accepted", accepted.status_code < 300, {"http_code": accepted.status_code})
            if accepted.status_code < 300:
                replacement_state = terminal(accepted.json()["request_id"])
                facts("replacement", replace_payload, replacement_state)
                menu = (replacement_state.get("result_summary") or {}).get("menu_summary") or {}
                replaced_ids = {int(x) for x in menu.get("recipe_ids", [])}
                locked = {int(x) for x in initial_menu["recipe_ids"]} - {target}
                check("replacement_only_target_changed", replacement_state["status"] == "completed" and
                      locked <= replaced_ids and target not in replaced_ids and
                      len(replaced_ids) == len(initial_menu["recipe_ids"]))
                session_menu = client.get(f"/v1/sessions/{payload['session_id']}").json().get("current_menu") or {}
                stale = client.post("/v1/recommendation-requests", json=replacement(initial_menu, target))
                check("stale_menu_conflict", stale.status_code == 409 and stale.json().get("error") == "MENU_VERSION_CONFLICT")
                if replacement_state["status"] == "completed" and session_menu.get("recipe_ids"):
                    concurrent_payloads = [replacement(session_menu, int(x)) for x in session_menu["recipe_ids"][:2]]
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        concurrent_responses = list(pool.map(lambda body: client.post("/v1/recommendation-requests", json=body), concurrent_payloads))
                    outcomes = []
                    observations = []
                    for body, result in zip(concurrent_payloads, concurrent_responses):
                        if result.status_code < 300:
                            outcome = terminal(result.json()["request_id"])
                            observations.append((body, result, outcome))
                        else:
                            observations.append((body, result, None))
                    # Wait for the winner before retrying the same accepted loser.
                    for body, result, outcome in observations:
                        if outcome is not None:
                            if outcome["status"] == "recovery_required":
                                before = storage.load_request_acceptance_by_request_id(outcome["request_id"])
                                recovered = client.post("/v1/recommendation-requests", json=body)
                                check("concurrent_recovery_same_request", recovered.status_code < 300 and
                                      recovered.json().get("request_id") == outcome["request_id"])
                                recovered.raise_for_status()
                                outcome = terminal(outcome["request_id"])
                                after = storage.load_request_acceptance_by_request_id(outcome["request_id"])
                                check("concurrent_recovery_new_generation", after["execution_generation"] > before["execution_generation"])
                            facts("concurrent_replacement_" + str(len(outcomes) + 1), body, outcome)
                            outcomes.append({"http_code": result.status_code, "request_id": outcome["request_id"],
                                             "status": outcome["status"], "error_code": (outcome.get("error") or {}).get("code")})
                        else:
                            outcomes.append({"http_code": result.status_code, "error": result.json().get("error")})
                    report["concurrent_replacement"] = outcomes
                    check("concurrent_same_version_not_double_committed", sum(x.get("status") == "completed" for x in outcomes) <= 1)
                    check("concurrent_same_version_one_winner", sum(x.get("status") == "completed" for x in outcomes) == 1)
                    check("concurrent_requests_reached_terminal", all(x.get("status") != "recovery_required" for x in outcomes))
                    missing = replacement(session_menu, 2147483647)
                    # Refresh source version so this validates target ownership rather than stale version.
                    newest = client.get(f"/v1/sessions/{payload['session_id']}").json().get("current_menu") or {}
                    missing.update(source_plan_id=newest.get("plan_id"), source_menu_hash=newest.get("menu_hash"))
                    invalid = client.post("/v1/recommendation-requests", json=missing)
                    check("target_not_in_menu_rejected", invalid.status_code == 422 and invalid.json().get("error") == "TARGET_RECIPE_NOT_IN_MENU")
                else:
                    report["blocked"].append("Concurrent replacement requires a successful first replacement")
            report.setdefault("limitations", []).append("HTTP workflow storage failure injection not performed; transaction rollback is verified separately by component tests")

        payload, response = create("推荐三道晚餐，所有菜品合计必须在1分钟内完成；不能满足时请让我选择放宽时间或减少菜数")
        state = terminal(response["request_id"])
        facts("clarification", payload, state)
        check("real_clarification_produced", state["status"] == "needs_clarification")
        if state["status"] == "needs_clarification":
            reply_state = state
            for attempt in range(3):
                if reply_state["status"] != "needs_clarification":
                    break
                question = reply_state["active_clarification"]
                time_option = next((o for o in question["options"] if "时间" in o["text"]), question["options"][0])
                # Fresh retrieval can exceed a previous minimum-time estimate.
                # Select the actual offered relaxation; never silently change constraints.
                option = next((o for o in question["options"]
                               if "取消" in o["text"] and "时间" in o["text"]), time_option) if attempt > 0 else time_option
                reply = {**payload, "idempotency_key": "live-audit-" + uuid.uuid4().hex,
                         "message": option["text"], "clarification_response": {
                             "question_id": question["question_id"], "option_id": option["option_id"]}}
                accepted = client.post("/v1/recommendation-requests", json=reply)
                accepted.raise_for_status()
                reply_state = terminal(accepted.json()["request_id"])
                facts("clarification_reply_" + str(attempt + 1), reply, reply_state)
            check("clarification_reply_completed", reply_state["status"] == "completed")
        else:
            report["blocked"].append("Structured clarification reply requires a real active question; clarification request did not produce one")

        payload, response = create("请推荐八道家常晚餐菜，尽量丰富")
        cancelled = client.post(f"/v1/recommendation-requests/{response['request_id']}/cancel")
        report["cancel_response"] = {"http_status": cancelled.status_code, "status": cancelled.json().get("status"),
                                     "error": cancelled.json().get("error")}
        state = terminal(response["request_id"])
        facts("cancellation", payload, state)
        report["cancelled_without_menu"] = state["status"] == "cancelled" and not (
            (state.get("result_summary") or {}).get("menu_summary"))
        check("cancelled_without_menu", report["cancelled_without_menu"])

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved=" + str(OUTPUT), flush=True)
    return 1 if report["blocked"] or any(not result["passed"] for result in report["checks"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
