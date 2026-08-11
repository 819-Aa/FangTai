"""T20 请求校验/幂等/participant_ref 测试。

无效请求绝不启动任务：校验先于任何状态写入/工作流触发。
"""

import uuid

import pytest

from food_agent_v2.d1 import api
from food_agent_v2.d1.schemas import validate_create_request


def _valid_body() -> dict:
    return {
        "idempotency_key": f"ik_{uuid.uuid4().hex[:8]}",
        "participants": [{"participant_ref": "p1", "user_id": 1}],
        "message": "推荐家常菜",
        "config": {},
    }


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(api, "_trigger_workflow", lambda *a, **k: None)
    yield
    api._requests.clear()
    api._idempotency.clear()
    api._events.clear()
    api._event_cursors.clear()


class TestValidateCreateRequest:
    def test_valid_passes(self) -> None:
        assert validate_create_request(_valid_body()) is None

    def test_missing_idempotency_key(self) -> None:
        body = _valid_body()
        del body["idempotency_key"]
        err = validate_create_request(body)
        assert err and any(e["field"] == "idempotency_key" for e in err["details"])

    def test_missing_participants(self) -> None:
        body = _valid_body()
        body["participants"] = []
        err = validate_create_request(body)
        assert err and any(e["field"] == "participants" for e in err["details"])

    def test_duplicate_participant_ref(self) -> None:
        body = _valid_body()
        body["participants"] = [{"participant_ref": "p1"}, {"participant_ref": "p1"}]
        err = validate_create_request(body)
        assert err and any("duplicate participant_ref" in e["issue"] for e in err["details"])

    def test_participant_missing_ref(self) -> None:
        body = _valid_body()
        body["participants"] = [{"user_id": 1}]
        err = validate_create_request(body)
        assert err and any("participant_ref" in e["field"] for e in err["details"])

    def test_missing_message(self) -> None:
        body = _valid_body()
        del body["message"]
        err = validate_create_request(body)
        assert err and any(e["field"] == "message" for e in err["details"])


class TestCreateRequest:
    def test_valid_request_accepted(self) -> None:
        code, resp = api.create_request(_valid_body())
        assert code == 202
        assert resp["status"] == "accepted"
        assert len(resp["request_id"]) == 36  # 完整 UUID（C3 工具回执按 UUID 绑定）

    def test_invalid_request_returns_422(self) -> None:
        body = _valid_body()
        body["participants"] = [{"user_id": 1}]  # 缺 participant_ref
        code, resp = api.create_request(body)
        assert code == 422
        assert resp["error"] == "VALIDATION_FAILED"

    def test_invalid_request_does_not_start_task(self) -> None:
        api._requests.clear()
        body = _valid_body()
        body["message"] = ""
        api.create_request(body)
        assert len(api._requests) == 0  # 未写入任何请求状态

    def test_idempotent_same_payload_returns_existing(self) -> None:
        body = _valid_body()
        code1, resp1 = api.create_request(body)
        assert code1 == 202
        code2, resp2 = api.create_request(body)
        assert code2 == 200
        assert resp2["request_id"] == resp1["request_id"]

    def test_idempotency_key_reused_different_payload_conflicts(self) -> None:
        body = _valid_body()
        api.create_request(body)
        body["message"] = "另一条消息"
        code, resp = api.create_request(body)
        assert code == 409
        assert resp["error"] == "IDEMPOTENCY_KEY_REUSED"
