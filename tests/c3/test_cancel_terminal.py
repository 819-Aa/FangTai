"""A graph cancellation must reach storage without becoming an internal failure."""
import uuid

from food_agent_v2.c3.graph_orchestrator import LangGraphRecommendationOrchestrator
from food_agent_v2.c3.state import RequestStatus, WorkflowState


def test_cancelled_graph_exit_preserves_terminal_and_finalizes(monkeypatch):
    runner = object.__new__(LangGraphRecommendationOrchestrator)
    finalized = []
    monkeypatch.setattr(runner, "_finalize", lambda state, *args: finalized.append(state))
    rid = str(uuid.uuid4())
    state = WorkflowState(request_id=rid, status=RequestStatus.CANCELLED)
    result = runner._node_finish_error({
        "workflow_state": state, "request_id": rid, "c4": None, "lock_token": "token",
    })
    assert result["workflow_state"].status == RequestStatus.CANCELLED
    assert finalized[0].status == RequestStatus.CANCELLED
    assert finalized[0].error is None
