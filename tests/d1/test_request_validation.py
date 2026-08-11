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
        # 公共接口只提交匿名 participant_ref/label，不携带 user_id
        "participants": [{"participant_ref": "p1", "label": "参与者 1"}],
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
        body["participants"] = [{"label": "参与者 1"}]  # 缺 participant_ref
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
        body["participants"] = [{"label": "参与者 1"}]  # 缺 participant_ref
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


class TestParticipantMapping:
    """匿名 participant_ref → 内部固定 user_id 的服务端确定性映射（T21）。"""

    def test_anonymous_refs_map_to_user_ids(self) -> None:
        """仅提交 participant_ref/label → 工作流收到 p1→1、p2→2 的内部映射。"""
        body = _valid_body()
        body["participants"] = [
            {"participant_ref": "p1", "label": "参与者 1"},
            {"participant_ref": "p2"},
        ]
        code, resp = api.create_request(body)
        assert code == 202
        stored = api._requests[resp["request_id"]]["participants"]
        assert stored[0]["participant_ref"] == "p1"
        assert stored[0]["label"] == "参与者 1"
        assert stored[0]["user_id"] == 1
        assert stored[1]["participant_ref"] == "p2"
        assert stored[1]["user_id"] == 2
        # 与 C3 runner 内部契约一致：p["user_id"] 可直接 int()（无 KeyError）
        mapping = {p["participant_ref"]: int(p["user_id"]) for p in stored}
        assert mapping == {"p1": 1, "p2": 2}
        # 公共响应不含 user_id
        assert "user_id" not in resp

    def test_invalid_or_out_of_range_ref_returns_422(self) -> None:
        """p0/p51/非法格式 → 启动工作流前 422，不写入请求状态。"""
        for bad in ("p0", "p51", "p01", "abc", "P1", ""):
            body = _valid_body()
            body["participants"] = [{"participant_ref": bad}]
            code, resp = api.create_request(body)
            assert code == 422, bad
            assert resp["error"] == "VALIDATION_FAILED"
        assert len(api._requests) == 0  # 未启动工作流/未写入请求

    def test_duplicate_ref_returns_422(self) -> None:
        body = _valid_body()
        body["participants"] = [{"participant_ref": "p1"},
                                {"participant_ref": "p1"}]
        code, resp = api.create_request(body)
        assert code == 422
        assert any("重复" in e["issue"] or "duplicate" in e["issue"]
                   for e in resp["details"])

    def test_user_id_submission_rejected(self) -> None:
        """公共接口禁止提交 user_id（只允许匿名 participant_ref）。"""
        body = _valid_body()
        body["participants"] = [{"participant_ref": "p1", "user_id": 1}]
        code, resp = api.create_request(body)
        assert code == 422
        assert any("user_id" in e["field"] for e in resp["details"])
        assert len(api._requests) == 0

    def test_workflow_receives_mapped_user_ids(self) -> None:
        """端到端边界：_trigger_workflow 收到的参与者即映射后的增强列表。"""
        captured: list[list[dict]] = []
        orig = api._trigger_workflow

        def fake_trigger(request_id, session_id, body):
            captured.append(api._requests[request_id]["participants"])

        api._trigger_workflow = fake_trigger
        try:
            body = _valid_body()
            body["participants"] = [{"participant_ref": "p3"},
                                    {"participant_ref": "p1"}]
            code, _ = api.create_request(body)
            assert code == 202
        finally:
            api._trigger_workflow = orig
        assert len(captured) == 1
        mapping = {p["participant_ref"]: int(p["user_id"]) for p in captured[0]}
        assert mapping == {"p3": 3, "p1": 1}
