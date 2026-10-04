import json
from uuid import uuid4

import pytest

from food_agent_v2.c3.tool_handler import ToolContext, ToolHandler
from food_agent_v2.d1 import RecommendationAPI


@pytest.fixture
def publisher(monkeypatch):
    p = RecommendationAPI()
    monkeypatch.setattr(p, "_persist_request", lambda *a, **k: True)
    monkeypatch.setattr(p, "_notify_sse", lambda *a: None)
    p._requests["r"] = {"execution_generation": 1}
    return p


def data(p):
    return [json.loads(e["data"]) for e in p._events["r"]]


def test_progress_projects_untrusted_text_to_fixed_templates(publisher):
    p = publisher
    p.publish_thought_node(
        "r",
        "candidate_search",
        "张三的内部记录",
        "done",
        summary="secret-marker arbitrary text",
        tool_name="search_candidates",
    )
    p.publish_tool_trace(
        "r", "search_candidates", "secret-marker results", node_id="candidate_search"
    )
    p.publish_analysis_event(
        "r", "retrieval", "secret-marker phase", ["unapproved-private-reference"]
    )
    encoded = json.dumps(data(p), ensure_ascii=False)
    assert "secret-marker" not in encoded
    assert "张三" not in encoded
    assert "unapproved-private-reference" not in encoded
    assert data(p)[0]["title"] == "菜品候选检索"
    assert data(p)[0]["summary"] == "菜品候选检索已完成"
    assert data(p)[1]["result_summary"] == "候选检索已完成"


def test_attempt_and_generation_identity_do_not_collide(publisher):
    p = publisher
    invocation = p.publish_thought_node(
        "r", "candidate_search", "x", "running", tool_name="search_candidates"
    )
    p.publish_tool_trace(
        "r", "search_candidates", "x", node_id="candidate_search", invocation_id=invocation
    )
    p.publish_thought_node("r", "candidate_search", "x", "done", invocation_id=invocation)
    assert invocation
    assert {d["invocation_id"] for d in data(p)} == {invocation}
    assert {d["execution_generation"] for d in data(p)} == {1}
    count = len(p._events["r"])
    p.publish_tool_trace(
        "r", "search_candidates", "x", node_id="candidate_search", invocation_id=invocation
    )
    assert len(p._events["r"]) == count
    p._requests["r"]["execution_generation"] = 2
    p.publish_thought_node("r", "candidate_search", "x", "running", invocation_id=invocation)
    assert data(p)[-1]["execution_generation"] == 2
    assert len({e["id"] for e in p._events["r"]}) == count + 1


def test_receipt_uses_preallocated_public_tool_identity(monkeypatch):
    from food_agent_v2.c3 import tool_handler as module

    monkeypatch.setitem(module._TOOL_MAP, "probe", lambda a, c: {"ok": True})
    ctx = ToolContext(request_id=str(uuid4()), node_id="node", build_id=str(uuid4()))
    ctx.progress_invocation_id = "preallocated-call"
    handler = ToolHandler(ctx)
    handler.execute("probe", {})
    handler.execute("probe", {})
    assert handler.receipts[0]["tool_call_id"] == "preallocated-call"
    assert handler.receipts[1]["tool_call_id"] != "preallocated-call"


def test_unstructured_invocation_identity_is_rejected(publisher):
    with pytest.raises(ValueError, match="invocation"):
        publisher.publish_thought_node(
            "r", "candidate_search", "x", "running", invocation_id="张三的调用"
        )
