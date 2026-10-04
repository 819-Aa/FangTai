"""Regression probe for the cancellation terminal overwritten during graph exit."""
import uuid

from food_agent_v2.c3.graph_orchestrator import LangGraphRecommendationOrchestrator
from food_agent_v2.c3.state import RequestStatus, WorkflowState


def test_c04_cancelled_graph_exit_must_preserve_cancelled(monkeypatch):
    # Run the actual exit-node logic; isolate only its final storage write.
    runner = object.__new__(LangGraphRecommendationOrchestrator)
    monkeypatch.setattr(runner, "_finalize", lambda *args, **kwargs: None)
    request_id = str(uuid.uuid4())
    workflow = WorkflowState(request_id=request_id, status=RequestStatus.CANCELLED)
    result = runner._node_finish_error({
        "workflow_state": workflow, "request_id": request_id,
        "c4": None, "lock_token": "audit-token",
    })
    assert result["workflow_state"].status == RequestStatus.CANCELLED
