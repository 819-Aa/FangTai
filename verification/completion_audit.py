"""Completion checks beyond the original A01-A10 regression probes."""
import json

import pytest

from food_agent_v2.d1 import RecommendationAPI, SensitiveDataBlocked


def test_c03_chinese_private_health_details_must_not_be_published(monkeypatch):
    service = RecommendationAPI()
    monkeypatch.setattr(service, "_persist_request", lambda *args, **kwargs: True)
    monkeypatch.setattr(service, "_notify_sse", lambda *args: None)
    # Synthetic person and health details; no real patient data.
    private_summary = "合成测试：测试成员甲患高血压，血压140，过敏史为花生过敏"
    try:
        service.publish_thought_node("completion-audit", "recipe_audit", "健康合规审查",
                                     "error", summary=private_summary)
    except SensitiveDataBlocked:
        return
    published = [json.loads(event["data"]) for event in service._events["completion-audit"]]
    print("C03 observed", published)
    pytest.fail("Synthetic Chinese health details were emitted in a public thought_node")
