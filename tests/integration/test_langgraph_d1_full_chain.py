"""LangGraph Agent 编排端到端链路集成测试。

验证从 D1 API (POST /v1/recommendation-requests) 发起请求开始，
通过 WORKFLOW_MODE="langgraph" 路由到 LangGraphRecommendationOrchestrator，
经历 Agent 行动-观察多轮循环、工具执行、健康规则审查、最终健康校验回执、
双产物构建与事务性终态提交，直至 D1 状态更新与 SSE 事件发布的完整闭环。
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from food_agent_v2.c3.agent_actions import ActionType, AgentAction
from food_agent_v2.c3.graph_orchestrator import DeterministicScriptedAgentModel
from food_agent_v2.c3.tools import ToolResponse
from food_agent_v2.c4 import ContextService
from food_agent_v2.d1 import api
from food_agent_v2.d1.schemas import SSEEventType


class _FastMockLLM(DeterministicScriptedAgentModel):
    """显式注入确定性脚本化模型，跳过网络调用以执行确定性闭环决策。"""
    pass


class _AlwaysClarifyModel:
    def decide_action(self, _state) -> AgentAction:
        return AgentAction(
            action=ActionType.ASK_USER,
            arguments={
                "inquiry_category": "NEEDS_CLARIFICATION",
                "reason": "请确认下一步偏好",
                "options": [
                    {"option_id": 1, "text": "选项 1：清淡", "modifications": {"taste_tags": ["清淡"]}},
                    {"option_id": 2, "text": "选项 2：家常", "modifications": {"taste_tags": ["家常"]}},
                ],
            },
            summary="继续向用户确认偏好",
        )


def _unique_key(prefix: str = "ik_lg") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _mock_retrieval_response() -> ToolResponse[dict[str, Any]]:
    return ToolResponse(
        success=True,
        status="ok",
        data={
            "candidates": [1, 2, 3],
            "retrieval_result": MagicMock(
                candidates=[
                    MagicMock(recipe_id=1, score=0.9, title="清炒西兰花"),
                    MagicMock(recipe_id=2, score=0.85, title="西红柿炒鸡蛋"),
                    MagicMock(recipe_id=3, score=0.8, title="冬瓜排骨汤"),
                ]
            ),
            "total": 3,
        },
    )


class TestLangGraphD1FullChain:
    """D1 API 到 LangGraph Agent 编排器全链路集成测试。"""

    def test_d1_api_langgraph_full_chain_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试正常推荐全链路：POST -> Agent 多轮决策 -> 终态 completed -> 产物与 SSE 完整。"""
        monkeypatch.setenv("WORKFLOW_MODE", "langgraph")

        mock_search = _mock_retrieval_response()

        with (
            patch("food_agent_v2.c3.graph_orchestrator.search_candidates", return_value=mock_search),
            patch("food_agent_v2.c3.runtime.get_llm_client", return_value=_FastMockLLM()),
        ):
            key = _unique_key("happy")
            body = {
                "idempotency_key": key,
                "participants": [{"participant_ref": "p1", "label": "用户 1"}],
                "message": "推荐3道菜",
                "config": {},
            }

            # 1. 发起请求
            code, resp = api.create_request(body)
            assert code == 202
            assert resp["status"] == "accepted"
            request_id = resp["request_id"]
            session_id = resp["session_id"]
            assert request_id and session_id

            # 2. 轮询直到终态 (最多等待 20 秒)
            terminal_status = None
            final_status_data = None
            for _ in range(40):
                time.sleep(0.5)
                st_code, st = api.get_request_status(request_id)
                assert st_code == 200
                cur_status = st.get("status")
                if cur_status in ("completed", "needs_clarification", "failed"):
                    terminal_status = cur_status
                    final_status_data = st
                    break

            # 3. 校验最终业务状态
            assert terminal_status == "completed", f"预期 completed，实际状态为 {terminal_status}: {final_status_data}"
            assert final_status_data is not None
            assert final_status_data["error"] is None

            result_summary = final_status_data.get("result_summary")
            assert result_summary is not None, "completed 状态必须携带 result_summary"
            menu_summary = result_summary.get("menu_summary")
            assert menu_summary is not None
            assert "items" in menu_summary
            assert len(menu_summary["items"]) == 3

            # 4. 校验 SSE 事件流完整性
            # completed 状态先写入；成功 outbox 在会话锁释放后才发布。
            event_types = []
            for _ in range(20):
                events = api._events.get(request_id, [])
                event_types = [ev.get("event") for ev in events]
                if SSEEventType.RESULT_COMMITTED.value in event_types:
                    break
                time.sleep(0.25)

            # 必须包含请求接收、分析准备、答案开始、回答就绪、结果提交
            assert SSEEventType.REQUEST_ACCEPTED.value in event_types
            assert SSEEventType.ANSWER_STARTED.value in event_types
            assert SSEEventType.ANSWER_READY.value in event_types
            assert SSEEventType.RESULT_COMMITTED.value in event_types

            # 且最终事件必须为 result_committed
            assert event_types[-1] == SSEEventType.RESULT_COMMITTED.value

    def test_followup_clarification_survives_redis_consume_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真实 D1/C4/MySQL/Redis：二次追问提交后 Redis 消费故障不覆盖终态。"""
        monkeypatch.setenv("WORKFLOW_MODE", "langgraph")
        original_consume = ContextService.consume_pending_clarification

        def wait_terminal(request_id: str) -> dict:
            for _ in range(200):
                code, result = api.get_request_status(request_id)
                assert code == 200
                if result["status"] in ("completed", "needs_clarification", "failed"):
                    time.sleep(0.3)  # 终态先于 worker 释放会话锁写入
                    return result
                time.sleep(0.1)
            pytest.fail(f"请求 {request_id} 未在 20 秒内到达终态")

        with patch("food_agent_v2.c3.runtime.get_llm_client", return_value=_AlwaysClarifyModel()):
            code, first = api.create_request({
                "idempotency_key": _unique_key("followup_1"),
                "participants": [{"participant_ref": "p1", "label": "用户 1"}],
                "message": "推荐三道晚餐",
                "config": {},
            })
            assert code == 202
            session_id = first["session_id"]
            assert wait_terminal(first["request_id"])["status"] == "needs_clarification"
            old_pending = ContextService().get_pending_clarifications(session_id)
            assert len(old_pending) == 1
            old_qid = old_pending[0]["question_id"]

            def fail_old_consume(self, sid, question_id=None, token=None):
                if sid == session_id and question_id == old_qid:
                    raise RuntimeError("injected Redis consume failure")
                return original_consume(self, sid, question_id=question_id, token=token)

            with patch.object(ContextService, "consume_pending_clarification", fail_old_consume):
                code, second = api.create_request({
                    "idempotency_key": _unique_key("followup_2"),
                    "session_id": session_id,
                    "participants": [{"participant_ref": "p1", "label": "用户 1"}],
                    "message": "选第一个",
                    "config": {},
                })
                assert code == 202
                assert wait_terminal(second["request_id"])["status"] == "needs_clarification"

            c4 = ContextService()
            assert c4.is_clarification_committed(session_id, old_qid)
            pending = c4.get_pending_clarifications(session_id)
            assert old_qid in {item["question_id"] for item in pending}
            assert len(pending) == 2

            code, third = api.create_request({
                "idempotency_key": _unique_key("followup_3"),
                "session_id": session_id,
                "participants": [{"participant_ref": "p1", "label": "用户 1"}],
                "message": "重新推荐三道晚餐",
                "config": {},
            })
            assert code == 202
            assert wait_terminal(third["request_id"])["status"] == "needs_clarification"
            assert old_qid not in {
                item["question_id"] for item in ContextService().get_pending_clarifications(session_id)
            }

    def test_d1_api_langgraph_full_chain_time_limit_clarification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """测试时间超限协商追问链路：POST (极端时间限制) -> Agent 规划受阻 -> 终态 needs_clarification。"""
        monkeypatch.setenv("WORKFLOW_MODE", "langgraph")

        mock_search = _mock_retrieval_response()

        with (
            patch("food_agent_v2.c3.graph_orchestrator.search_candidates", return_value=mock_search),
            patch("food_agent_v2.c3.runtime.get_llm_client", return_value=_FastMockLLM()),
        ):
            key = _unique_key("time_limit")
            body = {
                "idempotency_key": key,
                "participants": [{"participant_ref": "p1", "label": "用户 1"}],
                "message": "推荐3道菜，要求1分钟内做好",
                "config": {},
            }

            # 1. 发起请求
            code, resp = api.create_request(body)
            assert code == 202
            assert resp["status"] == "accepted"
            request_id = resp["request_id"]

            # 2. 轮询直到终态 (最多等待 20 秒)
            terminal_status = None
            final_status_data = None
            for _ in range(40):
                time.sleep(0.5)
                st_code, st = api.get_request_status(request_id)
                assert st_code == 200
                cur_status = st.get("status")
                if cur_status in ("completed", "needs_clarification", "failed"):
                    terminal_status = cur_status
                    final_status_data = st
                    break

            # 3. 校验协商追问终态
            assert terminal_status == "needs_clarification"
            assert final_status_data is not None

            error_info = final_status_data.get("error")
            assert error_info is not None
            assert error_info.get("code") == "TIME_LIMIT_EXCEEDED"

            message_text = error_info.get("message", "")
            # 必须包含给用户的结构化选项建议
            assert "选项 1" in message_text
            assert "选项 2" in message_text
            assert "选项 3" in message_text

            # 4. 校验 SSE 澄清事件
            event_types = []
            for _ in range(20):
                events = api.subscribe_events(request_id, refresh=True)
                event_types = [ev.get("event") for ev in events]
                if SSEEventType.CLARIFICATION_NEEDED.value in event_types:
                    break
                time.sleep(0.25)

            assert SSEEventType.REQUEST_ACCEPTED.value in event_types
            assert SSEEventType.CLARIFICATION_NEEDED.value in event_types
            assert SSEEventType.RESULT_COMMITTED.value not in event_types

    def test_d1_api_langgraph_full_chain_chinese_numeral_dish_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """测试中文数字菜数解析全链路：'三道清淡家常菜' -> dish_count=3 -> 终态 completed 且菜品数为 3。"""
        monkeypatch.setenv("WORKFLOW_MODE", "langgraph")

        mock_search = _mock_retrieval_response()

        with (
            patch("food_agent_v2.c3.graph_orchestrator.search_candidates", return_value=mock_search),
            patch("food_agent_v2.c3.runtime.get_llm_client", return_value=_FastMockLLM()),
        ):
            key = _unique_key("cn_numeral")
            body = {
                "idempotency_key": key,
                "participants": [{"participant_ref": "p1", "label": "用户 1"}],
                "message": "三道清淡家常菜",
                "config": {},
            }

            # 1. 发起请求
            code, resp = api.create_request(body)
            assert code == 202
            assert resp["status"] == "accepted"
            request_id = resp["request_id"]

            # 2. 轮询直到终态 (最多等待 20 秒)
            terminal_status = None
            final_status_data = None
            for _ in range(40):
                time.sleep(0.5)
                st_code, st = api.get_request_status(request_id)
                assert st_code == 200
                cur_status = st.get("status")
                if cur_status in ("completed", "needs_clarification", "failed"):
                    terminal_status = cur_status
                    final_status_data = st
                    break

            # 3. 校验最终业务状态为 completed，且菜品数严格为 3
            assert terminal_status == "completed", f"预期 completed，实际状态为 {terminal_status}: {final_status_data}"
            assert final_status_data is not None
            assert final_status_data["error"] is None

            result_summary = final_status_data.get("result_summary")
            assert result_summary is not None
            menu_summary = result_summary.get("menu_summary")
            assert menu_summary is not None
            assert len(menu_summary["items"]) == 3
