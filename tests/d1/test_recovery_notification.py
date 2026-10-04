"""Durable recovery state must be visible even when its Redis projection is lost."""
import json
from unittest.mock import MagicMock, patch

from food_agent_v2.d1 import RecommendationAPI


def test_recovery_get_and_sse_survive_missing_cache_and_replay():
    api = RecommendationAPI()
    c4 = MagicMock()
    c4.load_recommendation_log.return_value = None
    c4.load_dispatched_outbox_events.return_value = []
    c4.load_request_acceptance_by_request_id.return_value = {
        "request_id": "recover-r", "session_id": "recover-s",
        "status": "recovery_required", "execution_generation": 2,
        "created_at": "2026-10-03T00:00:00Z",
    }
    with patch("food_agent_v2.d1.ContextService", return_value=c4), \
         patch.object(api, "_restore_request"):
        code, status = api.get_request_status("recover-r")
        events = api.subscribe_events("recover-r")
        assert code == 200
        assert status["status"] == "recovery_required"
        assert status["session_id"] == "recover-s"
        assert status["error"]["code"] == "REQUEST_RECOVERY_REQUIRED"
        assert len(events) == 1
        assert events[0]["event"] == "request_recovery_required"
        assert json.loads(events[0]["data"])["request_id"] == "recover-r"
        assert api.subscribe_events("recover-r") == events
        assert api.subscribe_events("recover-r", events[0]["id"]) == []
