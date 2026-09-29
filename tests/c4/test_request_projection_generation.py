"""An old execution generation cannot overwrite a newer Redis projection."""

import json

from food_agent_v2.c4.redis_store import RedisSessionStore


def test_request_projection_rejects_older_generation(monkeypatch) -> None:
    store = RedisSessionStore()
    values = {}

    class FakeRedis:
        def eval(self, _lua, count, state_key, generation_key, generation, value, _ttl):
            assert count == 2
            current = values.get(generation_key)
            if current is not None and int(current) > int(generation):
                return 0
            values[state_key] = value
            values[generation_key] = str(generation)
            return 1

    store._client = FakeRedis()
    monkeypatch.setattr(store, "_connect", lambda: None)
    newer = {"state": {"status": "running", "execution_generation": 2}}
    older = {"state": {"status": "failed", "execution_generation": 1}}

    assert store.save_request_fenced("request-1", newer, 2) is True
    assert store.save_request_fenced("request-1", older, 1) is False
    assert json.loads(values[store._key("request", "request-1")]) == newer
