"""Verify a live v2 clarification and structured reply without printing private data."""

from __future__ import annotations

import json
import os
import sys
import uuid

os.environ["NO_PROXY"] = "localhost,127.0.0.1"
sys.path.insert(0, os.path.abspath("src"))

from food_agent_v2.c4 import ContextService  # noqa: E402
from food_agent_v2.d1 import api  # noqa: E402


def _terminal(request_id: str, timeout_seconds: int = 180) -> dict:
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        code, status = api.get_request_status(request_id)
        if code != 200:
            raise RuntimeError(f"GET {request_id} returned {code}: {status.get('error')}")
        if status.get("status") in {"completed", "needs_clarification", "failed", "cancelled", "interrupted"}:
            return status
        time.sleep(1)
    raise TimeoutError(request_id)


def _post(session_id: str, message: str, clarification_response: dict | None = None) -> tuple[str, dict]:
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "session_id": session_id,
        "participants": [{"participant_ref": "p1", "label": "用户 1"}],
        "message": message,
    }
    if clarification_response:
        body["clarification_response"] = clarification_response
    code, created = api.create_request(body)
    if code != 202:
        raise RuntimeError(f"POST returned {code}: {created.get('error')}")
    request_id = created["request_id"]
    print(f"created request_id={request_id} session_id={session_id}", flush=True)
    return request_id, _terminal(request_id)


def run_once() -> dict:
    c4 = ContextService()
    session_id = c4.create_session_record(
        ["p1"], workflow_mode="langgraph", clarification_protocol_version="v2",
    )
    first_id, first = _post(
        session_id,
        "推荐三道晚餐，所有菜品合计必须在1分钟内完成。如果无法满足，请先让我选择放宽时间或者减少菜数。",
    )
    question = first.get("active_clarification") or {}
    result = {
        "session_id": session_id,
        "first_request_id": first_id,
        "first_status": first.get("status"),
        "first_error_code": (first.get("error") or {}).get("code"),
        "first_log_status": (c4.load_recommendation_log(first_id) or {}).get("status"),
        "question_id": question.get("question_id"),
        "first_sse_types": [e.get("event") for e in api.subscribe_events(first_id, refresh=True)],
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if first.get("status") != "needs_clarification":
        return result

    options = question.get("options") or []
    if not question.get("question_id") or not options:
        raise AssertionError("needs_clarification must expose a question and options")
    option = options[0]
    second_id, second = _post(
        session_id, option["text"],
        {"question_id": question["question_id"], "option_id": option["option_id"]},
    )
    _, old = api.get_request_status(first_id)
    result.update({
        "second_request_id": second_id,
        "second_status": second.get("status"),
        "second_error_code": (second.get("error") or {}).get("code"),
        "second_log_status": (c4.load_recommendation_log(second_id) or {}).get("status"),
        "second_question_id": (second.get("active_clarification") or {}).get("question_id"),
        "old_request_active": old.get("active_clarification"),
        "second_sse_types": [e.get("event") for e in api.subscribe_events(second_id, refresh=True)],
    })
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    for attempt in range(1, 4):
        print(f"attempt={attempt}", flush=True)
        outcome = run_once()
        if outcome.get("first_error_code") != "MODEL_INVOCATION_FAILED":
            break
    if outcome.get("first_status") != "needs_clarification":
        raise SystemExit("live structured clarification not observed")
