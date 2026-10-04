"""Real HTTP/model/MySQL rollback and generation-2 same-payload recovery."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

from food_agent_v2.application.commit_service import _connect
from food_agent_v2.c3.runtime import AgentRuntime
from food_agent_v2.c4 import ContextService
from food_agent_v2.d1.progress import NODE_TITLES, STAGE_NODES, STATUS_SUFFIX, TOOL_SUMMARIES
from verification.live_chain_audit import TERMINAL, sse

OUTPUT = Path(
    os.environ.get(
        "HTTP_COMMIT_FAULT_OUTPUT", "verification/artifacts/http-commit-fault-2026-10-04.json"
    )
)
CONTROL = Path(os.environ["HTTP_COMMIT_FAULT_CONTROL"])
BASE = os.environ.get("HTTP_COMMIT_FAULT_BASE", "http://127.0.0.1:8004")
report = {"mock": False, "transport": "real HTTP", "checks": []}


def check(name, condition):
    report["checks"].append({"name": name, "passed": bool(condition)})
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(name, bool(condition), flush=True)
    assert condition, name


def wait(client, rid):
    for _ in range(300):
        response = client.get(f"/v1/recommendation-requests/{rid}")
        response.raise_for_status()
        state = response.json()
        if state["status"] in TERMINAL:
            return state
        time.sleep(1)
    raise TimeoutError(rid)


def snapshot(sid, rid):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT current_menu_plan_id, request_count, fencing_token, "
            "active_clarification_question_id, clarification_revision FROM sessions WHERE session_id=%s",
            (sid,),
        )
        facts = {"session": list(cur.fetchone())}
        cur.execute("SELECT COUNT(*) FROM menu_versions WHERE session_id=%s", (sid,))
        facts["version_count"] = cur.fetchone()[0]
        for table in ("recommendation_logs", "outbox", "conversation_events"):
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE request_id=%s", (rid,))
            facts[table] = cur.fetchone()[0]
        return facts
    finally:
        conn.close()


def main():
    storage = ContextService()
    with httpx.Client(base_url=BASE, trust_env=False, timeout=20) as client:
        client.get("/ready").raise_for_status()
        participants = [{"participant_ref": "p1"}]
        sid = client.post("/v1/sessions", json={"participants": participants}).json()["session_id"]
        payload = {
            "session_id": sid,
            "participants": participants,
            "idempotency_key": uuid.uuid4().hex,
            "message": "今晚晚餐，请推荐三道清淡家常菜，45分钟内完成",
        }
        initial = client.post("/v1/recommendation-requests", json=payload)
        initial.raise_for_status()
        state = wait(client, initial.json()["request_id"])
        report.update(session_id=sid, initial_request_id=state["request_id"])
        check("initial_real_menu", state["status"] == "completed")
        menu = state["result_summary"]["menu_summary"]
        target = menu["recipe_ids"][0]
        replacement = {
            **payload,
            "idempotency_key": uuid.uuid4().hex,
            "message": "只替换这道菜，保持其他菜品不变，清淡家常口味",
            "action": "replace_dish",
            "target_recipe_id": target,
            "source_plan_id": menu["plan_id"],
            "source_menu_hash": menu["menu_hash"],
        }
        # Arm before POST using the unique key: acceptance request_id is read by
        # the client immediately, long before the model completes and commits.
        response = client.post("/v1/recommendation-requests", json=replacement)
        response.raise_for_status()
        rid = response.json()["request_id"]
        CONTROL.write_text(json.dumps({"request_id": rid}), encoding="utf-8")
        report["request_id"] = rid
        before = snapshot(sid, rid)
        failed = wait(client, rid)
        after = snapshot(sid, rid)
        report.update(
            before=before,
            after_rollback=after,
            fault_state=failed,
            injection=json.loads(CONTROL.read_text(encoding="utf-8")),
        )
        check(
            "fault_was_injected_after_real_write",
            report["injection"].get("after_real_outbox_insert")
            and report["injection"].get("rollback_seen"),
        )
        check("http_reports_recovery_required", failed["status"] == "recovery_required")
        check(
            "transaction_rolled_back_all_facts",
            before == after
            and after["outbox"] == 0
            and after["recommendation_logs"] == 0
            and after["conversation_events"] == 0,
        )
        current = client.get(f"/v1/sessions/{sid}").json()["current_menu"]
        check(
            "old_menu_preserved",
            current["plan_id"] == menu["plan_id"] and current["menu_hash"] == menu["menu_hash"],
        )
        prior_events = sse(client, rid)
        check(
            "no_false_completion",
            not any(e["event"] in ("answer_ready", "result_committed") for e in prior_events),
        )
        retry = client.post("/v1/recommendation-requests", json=replacement)
        retry.raise_for_status()
        check("recovery_keeps_request_id", retry.json()["request_id"] == rid)
        completed = wait(client, rid)
        report["recovered_state"] = completed
        acceptance = storage.load_request_acceptance_by_request_id(rid)
        check(
            "same_payload_generation_two_completed",
            completed["status"] == "completed" and acceptance["execution_generation"] == 2,
        )
        updated = completed["result_summary"]["menu_summary"]
        current = client.get(f"/v1/sessions/{sid}").json()["current_menu"]
        check(
            "replacement_preserves_other_dishes",
            target not in updated["recipe_ids"]
            and set(menu["recipe_ids"]) - {target} <= set(updated["recipe_ids"])
            and len(menu["recipe_ids"]) == len(updated["recipe_ids"]),
        )
        check(
            "mysql_http_menu_consistent",
            storage.load_recommendation_log(rid)["status"] == "completed"
            and current["plan_id"] == updated["plan_id"]
            and current["menu_hash"] == updated["menu_hash"],
        )
        for _ in range(40):
            events = sse(client, rid)
            if any(e["event"] == "result_committed" for e in events):
                break
            time.sleep(1)
        report["events"] = events
        check(
            "committed_result_delivered",
            any(
                e["event"] == "result_committed" and e["data"]["menu_summary"] == updated
                for e in events
            ),
        )
        log = storage.load_recommendation_log(rid)
        evidence = log["health_evidence"]
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        verify_progress(client, rid, replacement, events, evidence)


def verify_progress(client, rid, replacement, events, evidence):
    storage = ContextService()
    # The existing audit contract excludes final-validation receipts with
    # nondeterministic output IDs, recording their artifact instead. Check
    # persisted tool refs only for the tools covered by that contract.
    names = {
        "search_candidates": "retrieve_recipes",
        "audit_recipe_health": "evaluate_recipe_health",
        "combine_nutritional_menu": "generate_feasible_menus",
        "validate_selected_menu_health": "validate_selected_menu_health",
    }
    traces = [
        e["data"]
        for e in events
        if e["event"] == "tool_trace" and e["data"]["execution_generation"] == 2
    ]
    tool_ids = {
        e["invocation_id"]
        for e in traces
        if names[e["tool_name"]] not in AgentRuntime.NONDETERMINISTIC_OUTPUT_TOOLS
    }
    check(
        "public_invocations_match_persisted_tool_receipts",
        tool_ids and tool_ids <= set(evidence["tool_receipt_refs"]),
    )
    check(
        "all_tool_invocations_match_actual_node_attempts",
        all(
            any(
                e["event"] == "thought_node"
                and e["data"].get("invocation_id") == trace["invocation_id"]
                and e["data"].get("execution_generation") == 2
                and e["data"].get("status") == "running"
                for e in events
            )
            for trace in traces
        ),
    )
    progress = [e for e in events if e["event"] in ("thought_node", "tool_trace", "analysis_ready")]
    # D1 intentionally hides older execution generations after reclaim;
    # their progress cannot be mistaken for the new authoritative worker.
    check(
        "only_authoritative_generation_is_replayed",
        {e["data"]["execution_generation"] for e in progress} == {2}
        and len({e["id"] for e in events}) == len(events),
    )
    check(
        "fixed_public_progress",
        all(
            e["data"]["summary"]
            == NODE_TITLES[e["data"]["node_id"]] + STATUS_SUFFIX[e["data"]["status"]]
            if e["event"] == "thought_node"
            else e["data"]["result_summary"] == TOOL_SUMMARIES[e["data"]["tool_name"]]
            if e["event"] == "tool_trace"
            else e["data"]["summary"]
            in {
                NODE_TITLES[STAGE_NODES.get(e["data"]["stage"], "processing")] + suffix
                for suffix in STATUS_SUFFIX.values()
            }
            for e in progress
        ),
    )
    middle = len(events) // 2
    check(
        "last_event_id_exact_suffix", sse(client, rid, events[middle]["id"]) == events[middle + 1 :]
    )
    replay = client.post("/v1/recommendation-requests", json=replacement)
    check(
        "terminal_idempotent_replay",
        replay.status_code == 200
        and replay.json()["request_id"] == rid
        and storage.load_request_acceptance_by_request_id(rid)["execution_generation"] == 2,
    )
    report["complete"] = True
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--reconcile":
        original = Path(
            os.environ.get(
                "HTTP_COMMIT_FAULT_ORIGINAL",
                "verification/artifacts/http-commit-fault-2026-10-04.json",
            )
        )
        report = json.loads(original.read_text(encoding="utf-8"))
        OUTPUT = Path(
            os.environ.get(
                "HTTP_COMMIT_FAULT_OUTPUT",
                "verification/artifacts/http-commit-fault-reconciled-2026-10-04.json",
            )
        )
        report["original_report"] = str(original)
        report["checks"] = [c for c in report["checks"] if c["passed"]]
        rid = report["request_id"]
        log = ContextService().load_recommendation_log(rid)
        evidence = log["health_evidence"]
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        with httpx.Client(base_url=BASE, trust_env=False, timeout=20) as client:
            events = sse(client, rid)
            from food_agent_v2.c4.redis_store import RedisSessionStore

            saved = RedisSessionStore().load_request(rid)["state"]
            keys = (
                "session_id",
                "idempotency_key",
                "message",
                "action",
                "target_recipe_id",
                "source_plan_id",
                "source_menu_hash",
            )
            replacement = {k: saved[k] for k in keys}
            replacement["participants"] = [
                {"participant_ref": p["participant_ref"]} for p in saved["participants"]
            ]
            verify_progress(client, rid, replacement, events, evidence)
    else:
        main()
