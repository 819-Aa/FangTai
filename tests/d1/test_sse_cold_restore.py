import json
from unittest.mock import MagicMock

import pytest

from food_agent_v2 import d1
from food_agent_v2.c4.redis_store import RedisSessionStore


@pytest.mark.parametrize("previous_generation", [None, 1])
def test_refresh_restores_authoritative_request_identity_before_replaying_progress(
    monkeypatch,
    previous_generation,
):
    api = d1.RecommendationAPI()
    state = {
        "request_id": "r",
        "status": "completed",
        "execution_generation": 2,
        "execution_owner": "owner-2",
        "stage_events_cursor": 1,
    }
    event = {
        "id": "progress-g2",
        "event": "thought_node",
        "data": json.dumps({"execution_generation": 2, "node_id": "candidate_search"}),
    }
    if previous_generation:
        api._requests["r"] = {**state, "execution_generation": 1, "execution_owner": "owner-1"}
        api._events["r"] = [{"id": "old-progress", "event": "thought_node", "data": "{}"}]
    monkeypatch.setattr(
        RedisSessionStore,
        "load_request",
        lambda self, rid: {
            "state": state,
            "events": [event],
            "cursor": 2,
        },
    )
    storage = MagicMock()
    storage.load_request_acceptance_by_request_id.return_value = {**state, "status": "terminal"}
    storage.load_dispatched_outbox_events.return_value = []
    monkeypatch.setattr(d1, "ContextService", lambda: storage)
    assert api.subscribe_events("r", refresh=True) == [event]
    assert api._requests["r"]["execution_generation"] == 2
    assert api.subscribe_events("r", "progress-g2", refresh=True) == []
