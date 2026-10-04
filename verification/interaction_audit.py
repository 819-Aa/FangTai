"""Explicit audit probes; expected-contract failures identify unresolved defects.

Run: .venv/Scripts/python.exe -m pytest verification/interaction_audit.py -q -s
Persistence/notification are isolated; actual publishers and event de-duplication run.
"""
import json

import pytest

from food_agent_v2.d1 import RecommendationAPI, SensitiveDataBlocked
from food_agent_v2.d1.schemas import validate_create_request


@pytest.fixture
def publisher(monkeypatch):
    service = RecommendationAPI()
    monkeypatch.setattr(service, "_persist_request", lambda *args, **kwargs: True)
    monkeypatch.setattr(service, "_notify_sse", lambda *args: None)
    return service


def test_a05_sensitive_free_text_must_be_blocked(publisher):
    # Synthetic text, not a real person's record.
    with pytest.raises(SensitiveDataBlocked):
        publisher.publish_thought_node(
            "audit", "audit_node", "执行记录", "done",
            summary="synthetic patient: blood_pressure=140; disease_name=example",
        )


def test_a06_public_fields_must_be_allowlisted(publisher):
    with pytest.raises(SensitiveDataBlocked):
        publisher.publish_tool_trace(
            "audit", "search_candidates", "完成", unknown_internal_data="synthetic-private-marker",
        )


def test_a07_distinct_tool_executions_must_survive_replay(publisher):
    publisher.publish_tool_trace("audit", "search_candidates", "first search", node_id="search")
    publisher.publish_tool_trace("audit", "search_candidates", "expanded search", node_id="search")
    events = publisher._events["audit"]
    print("A07 observed", [json.loads(event["data"])["result_summary"] for event in events])
    assert len(events) == 2


def test_a08_distinct_node_attempts_must_survive_replay(publisher):
    for attempt in (1, 2):
        for status in ("running", "done"):
            publisher.publish_thought_node("audit", "audit_node", "阶段", status, summary=f"attempt {attempt}")
    events = publisher._events["audit"]
    print("A08 observed", [json.loads(event["data"])["summary"] for event in events])
    assert len(events) == 4


def test_a09_same_explicit_event_id_is_deduplicated(publisher):
    for _ in range(2):
        publisher.publish_tool_trace("audit", "search_candidates", "same fact", event_id="stable-fact")
    assert len(publisher._events["audit"]) == 1


def test_a10_replace_hash_must_be_hexadecimal():
    body = {
        "idempotency_key": "audit", "participants": [{"participant_ref": "p1"}],
        "message": "replace", "session_id": "audit", "action": "replace_dish",
        "target_recipe_id": 1, "source_plan_id": "plan", "source_menu_hash": "z" * 64,
    }
    error = validate_create_request(body)
    print("A10 observed", error)
    assert error is not None
