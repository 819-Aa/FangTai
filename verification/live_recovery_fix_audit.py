"""Real HTTP + MySQL/Redis checks for cancellation and lock-contention recovery.

Only creates isolated sessions and holds/releases its own session's lock.
"""
import json
import os
import time
import uuid
from pathlib import Path

import httpx

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
from food_agent_v2.c4 import ContextService
from verification.live_chain_audit import sse

output = Path(os.environ.get("LIVE_RECOVERY_OUTPUT", str(Path(__file__).parent / "artifacts/live-recovery-fixed-2026-10-03.json")))
report = {"mock": False, "checks": [], "requests": [], "complete": False}
storage = ContextService()


def check(name, passed, *, fatal=True, **details):
    report["checks"].append({"name": name, "passed": bool(passed), **details})
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["checks"][-1], ensure_ascii=True), flush=True)
    if fatal:
        assert passed, name


with httpx.Client(base_url="http://127.0.0.1:8003", trust_env=False, timeout=25) as client:
    client.get("/ready").raise_for_status()

    def payload():
        res = client.post("/v1/sessions", json={"participants": [{"participant_ref": "p1"}]})
        res.raise_for_status()
        return {"session_id": res.json()["session_id"], "participants": [{"participant_ref": "p1"}],
                "idempotency_key": "fix-audit-" + uuid.uuid4().hex,
                "message": "今晚晚餐，请推荐三道清淡家常菜，45分钟内完成"}

    def wait(rid, statuses, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            res = client.get(f"/v1/recommendation-requests/{rid}")
            res.raise_for_status()
            state = res.json()
            if state["status"] in statuses:
                return state
            time.sleep(0.5)
        raise TimeoutError(rid)

    # The original failure happened before the first model call.
    body = payload()
    res = client.post("/v1/recommendation-requests", json=body)
    res.raise_for_status()
    rid = res.json()["request_id"]
    report["requests"].append({"scenario": "early_cancel", "request_id": rid, "session_id": body["session_id"]})
    cancelled = client.post(f"/v1/recommendation-requests/{rid}/cancel")
    check("cancel_http_confirmed", cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled")
    deadline = time.monotonic() + 20
    log = None
    while time.monotonic() < deadline:
        log = storage.load_recommendation_log(rid)
        if log:
            break
        time.sleep(0.5)
    state = client.get(f"/v1/recommendation-requests/{rid}").json()
    events = sse(client, rid)
    check("cancel_get_mysql_consistent", state["status"] == "cancelled" and bool(log) and log["status"] == "cancelled",
          get_status=state["status"], mysql_status=log.get("status") if log else None)
    check("cancel_sse_without_failed", any(e["event"] == "request_cancelled" for e in events)
          and not any(e["event"] == "error" or (e["event"] == "request_terminal" and e["data"].get("status") == "failed") for e in events))
    check("cancel_no_menu", client.get(f"/v1/sessions/{body['session_id']}").json().get("current_menu") is None)

    # Deterministically reproduce the same Redis lock competition as concurrent POSTs.
    body = payload()
    token = storage.acquire_session_lock(body["session_id"], "verification-recovery")
    assert token is not None
    try:
        res = client.post("/v1/recommendation-requests", json=body)
        res.raise_for_status()
        rid = res.json()["request_id"]
        report["requests"].append({"scenario": "lock_recovery", "request_id": rid, "session_id": body["session_id"]})
        state = wait(rid, {"recovery_required"}, 30)
        acceptance = storage.load_request_acceptance_by_request_id(rid)
        generation = acceptance["execution_generation"]
        check("recovery_get_reason", state["error"]["code"] == "REQUEST_RECOVERY_REQUIRED")
        events = sse(client, rid)
        check("recovery_sse_visible", any(e["event"] == "request_recovery_required" for e in events),
              event_types=[e["event"] for e in events])
        recovery_event = next(e for e in events if e["event"] == "request_recovery_required")
        check("recovery_sse_resume", sse(client, rid, recovery_event["id"]) == [])
        check("recovery_not_false_terminal", storage.load_recommendation_log(rid) is None)
    finally:
        storage.release_session_lock(body["session_id"], token)

    recovered = client.post("/v1/recommendation-requests", json=body)
    recovered.raise_for_status()
    check("recovery_same_request_id", recovered.json()["request_id"] == rid)
    state = wait(rid, {"completed", "failed", "needs_clarification", "no_safe_menu", "no_feasible_menu", "recovery_required"})
    acceptance = storage.load_request_acceptance_by_request_id(rid)
    check("recovery_new_generation", acceptance["execution_generation"] > generation,
          before=generation, after=acceptance["execution_generation"])
    check("recovery_real_menu_completed", state["status"] == "completed", fatal=False,
          status=state["status"], error=state.get("error"))
    menu = (state.get("result_summary") or {}).get("menu_summary")
    current = client.get(f"/v1/sessions/{body['session_id']}").json()["current_menu"]
    log = storage.load_recommendation_log(rid)
    check("recovery_terminal_consistent", bool(log) and log["status"] == state["status"] and acceptance["status"] == "terminal")
    if state["status"] == "completed":
        public_fields = ("build_id", "plan_id", "menu_hash", "recipe_ids", "items")
        check("recovery_get_mysql_session_consistent",
              all(menu.get(key) == current.get(key) for key in public_fields) and len(menu["recipe_ids"]) == 3)
    else:
        error = state.get("error") or {}
        if error.get("code") == "RETRIEVAL_FAILED" and "ConnectError" in error.get("message", ""):
            report["network_blocked"] = "SiliconFlow /embeddings ConnectError"
        check("recovery_failure_no_phantom_menu", current is None)
    replay = client.post("/v1/recommendation-requests", json=body)
    check("recovery_terminal_replay_idempotent", replay.status_code == 200 and replay.json()["request_id"] == rid)
    different = client.post("/v1/recommendation-requests", json={**body, "message": "改成两道菜"})
    check("recovery_payload_conflict", different.status_code == 409)
    report["complete"] = all(item["passed"] for item in report["checks"])
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not report["complete"]:
        raise SystemExit(1)
