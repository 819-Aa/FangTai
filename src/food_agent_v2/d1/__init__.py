"""D1 API 与 SSE —— 对外 HTTP 接口适配层。

将 HTTP 请求适配为 C3 工作流调用，SSE 事件序列化为客户端可消费的 JSON。
不包含业务逻辑——只是 REST 到 C3 的翻译层。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from food_agent_v2.d1.schemas import (
    validate_create_request, scan_forbidden_fields,
    now_iso, RequestStatus, SSEEventType,
)


class RecommendationAPI:
    """推荐 API 端点处理。"""

    def __init__(self):
        self._requests: dict[str, dict] = {}             # request_id → request state
        self._idempotency: dict[str, dict] = {}          # idempotency_key → {payload_hash, request_id}
        self._events: dict[str, list[dict]] = {}         # request_id → SSE events
        self._event_cursors: dict[str, int] = {}          # request_id → next event_id

    # ---- Redis 持久化（API 重启后请求状态/SSE/幂等不丢；文档 §20：Redis 管运行时） ----

    def _persist_request(self, request_id: str) -> None:
        if request_id not in self._requests:
            return
        from food_agent_v2.c4.redis_store import RedisSessionStore
        try:
            RedisSessionStore().save_request(request_id, {
                "state": self._requests[request_id],
                "events": self._events.get(request_id, []),
                "cursor": self._event_cursors.get(request_id, 1),
            })
        except Exception:
            pass  # Redis 不可用时降级为内存

    def _restore_request(self, request_id: str) -> None:
        if request_id in self._requests:
            return
        from food_agent_v2.c4.redis_store import RedisSessionStore
        try:
            blob = RedisSessionStore().load_request(request_id)
        except Exception:
            return
        if not blob:
            return
        self._requests[request_id] = blob.get("state", {})
        self._events[request_id] = blob.get("events", [])
        self._event_cursors[request_id] = blob.get("cursor", 1)
        # 幂等键恢复（含 payload_hash，保证幂等判定完整）
        key = blob.get("state", {}).get("idempotency_key")
        rid = blob.get("state", {}).get("request_id")
        phash = blob.get("state", {}).get("payload_hash")
        if key and rid:
            self._idempotency[key] = {"payload_hash": phash, "request_id": rid}

    # ---- POST /v1/recommendation-requests ----

    def create_request(self, body: dict) -> tuple[int, dict]:
        """创建推荐请求。返回 (status_code, response_body)。"""
        # 校验
        error = validate_create_request(body)
        if error:
            return 422, error

        key = body["idempotency_key"]
        payload_hash = self._hash_payload(body)

        # 幂等检查
        if key in self._idempotency:
            existing = self._idempotency[key]
            if existing["payload_hash"] == payload_hash:
                # 相同请求 → 返回已有
                req = self._requests.get(existing["request_id"])
                if req:
                    return 200, {
                        "request_id": req["request_id"],
                        "session_id": req["session_id"],
                        "status": req["status"],
                        "created_at": req["created_at"],
                        "result_summary": req.get("result_summary"),
                    }
            else:
                # 相同键不同载荷 → 冲突
                return 409, {
                    "error": "IDEMPOTENCY_KEY_REUSED",
                    "existing_request_id": existing["request_id"],
                }

        # 创建请求
        request_id = str(uuid.uuid4())[:8]
        session_id = body.get("session_id") or f"sess_{uuid.uuid4().hex[:8]}"
        now = now_iso()

        req_state = {
            "request_id": request_id,
            "session_id": session_id,
            "idempotency_key": key,
            "payload_hash": payload_hash,
            "status": "accepted",
            "created_at": now,
            "updated_at": now,
            "participants": body.get("participants", []),
            "message": body.get("message", ""),
            "config": body.get("config", {}),
            "stage_events_cursor": 0,
            "result_summary": None,
            "error": None,
        }

        self._requests[request_id] = req_state
        self._idempotency[key] = {"payload_hash": payload_hash, "request_id": request_id}
        self._events[request_id] = []
        self._event_cursors[request_id] = 1

        # 发送 request_accepted SSE 事件
        self._emit_event(request_id, SSEEventType.REQUEST_ACCEPTED, {
            "request_id": request_id,
            "session_id": session_id,
        })

        self._persist_request(request_id)

        # 异步触发工作流
        self._trigger_workflow(request_id, session_id, body)

        return 202, {
            "request_id": request_id,
            "session_id": session_id,
            "status": "accepted",
            "created_at": now,
        }

    # ---- GET /v1/recommendation-requests/{request_id} ----

    def get_request_status(self, request_id: str) -> tuple[int, dict]:
        """查询请求状态。"""
        self._restore_request(request_id)
        req = self._requests.get(request_id)
        if not req:
            return 404, {"error": "NOT_FOUND", "message": "request not found"}
        return 200, {
            "request_id": req["request_id"],
            "session_id": req["session_id"],
            "status": req["status"],
            "created_at": req["created_at"],
            "updated_at": req.get("updated_at", req["created_at"]),
            "stage_events_cursor": req.get("stage_events_cursor", 0),
            "result_summary": req.get("result_summary"),
            "error": req.get("error"),
        }

    # ---- GET /v1/recommendation-requests/{request_id}/events ----

    def subscribe_events(self, request_id: str, last_event_id: str | None = None) -> list[dict]:
        """获取 SSE 事件（自 last_event_id 之后）。"""
        self._restore_request(request_id)
        all_events = self._events.get(request_id, [])
        if last_event_id:
            try:
                start = int(last_event_id)
            except (ValueError, TypeError):
                start = 0
            return all_events[start:]
        return all_events

    def _emit_event(self, request_id: str, event_type: SSEEventType, payload: dict) -> None:
        """发布 SSE 事件。"""
        eid = self._event_cursors.get(request_id, 1)
        event = {
            "id": str(eid),
            "event": event_type.value,
            "data": json.dumps(payload, ensure_ascii=False),
        }
        self._events.setdefault(request_id, []).append(event)
        self._event_cursors[request_id] = eid + 1
        if request_id in self._requests:
            self._requests[request_id]["stage_events_cursor"] = eid
        self._persist_request(request_id)

    # ---- POST /v1/recommendation-requests/{request_id}/cancel ----

    def cancel_request(self, request_id: str) -> tuple[int, dict]:
        """取消请求。写 Redis 取消标记，工作流线程在节点边界检查并停止（文档 §15）。"""
        self._restore_request(request_id)
        req = self._requests.get(request_id)
        if not req:
            return 404, {"error": "NOT_FOUND"}

        terminal = {"completed", "failed", "no_safe_menu", "no_feasible_menu", "cancelled"}
        if req["status"] in terminal:
            return 409, {"error": "REQUEST_ALREADY_TERMINAL", "current_status": req["status"]}

        req["status"] = "cancelled"
        req["updated_at"] = now_iso()
        self._emit_event(request_id, SSEEventType.REQUEST_CANCELLED, {"request_id": request_id})

        # 取消标记：工作流在节点边界检查（TTL 1h）
        try:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            store = RedisSessionStore()
            store._connect()
            if store._client:
                store._client.set(f"v2:cancel:{request_id}", "1", ex=3600)
        except Exception:
            pass

        return 200, {
            "request_id": request_id,
            "status": "cancelled",
            "cancelled_at": now_iso(),
        }

    # ---- 工作流集成 ----

    def publish_clarification_event(self, request_id: str,
                                    query_plan: dict | None = None) -> None:
        """查询理解需要澄清时发布 clarificaton_needed 事件（文档 09 §8.2）。"""
        self._emit_event(request_id, SSEEventType.CLARIFICATION_NEEDED, {
            "request_id": request_id,
            "clarification": (query_plan or {}).get(
                "clarification_question", "请补充必要信息后再推荐"),
        })

    def publish_analysis_event(self, request_id: str, stage: str,
                               summary: str, evidence_refs: list[str]) -> None:
        """C3 通过此接口发布阶段分析事件。"""
        payload = scan_forbidden_fields({
            "stage": stage,
            "summary": summary,
            "evidence_refs": evidence_refs,
        })
        if scan_forbidden_fields(payload):
            return  # 禁止字段拦截
        self._emit_event(request_id, SSEEventType.ANALYSIS_READY, payload)

    def publish_answer_event(self, request_id: str, text: str,
                            menu_ref: str, evidence_refs: list[str]) -> None:
        """C3 通过此接口发布回答事件。"""
        violations = scan_forbidden_fields({
            "text": text, "menu_ref": menu_ref,
            "evidence_refs": evidence_refs,
        })
        if violations:
            self._emit_event(request_id, SSEEventType.ERROR, {
                "error_code": "SENSITIVE_DATA_EXPOSURE",
                "message": f"Forbidden fields: {violations}",
            })
            return
        self._emit_event(request_id, SSEEventType.ANSWER_READY, {
            "text": text,
            "menu_ref": menu_ref,
            "evidence_refs": evidence_refs,
        })

    def publish_result_committed(self, request_id: str, menu_summary: dict) -> None:
        self._emit_event(request_id, SSEEventType.RESULT_COMMITTED, {
            "request_id": request_id,
            "menu_summary": menu_summary,
        })

    def update_status(self, request_id: str, status: str,
                      result_summary: dict | None = None,
                      error: dict | None = None) -> None:
        if request_id in self._requests:
            self._requests[request_id]["status"] = status
            self._requests[request_id]["updated_at"] = now_iso()
            if result_summary:
                self._requests[request_id]["result_summary"] = result_summary
            if error is not None:
                self._requests[request_id]["error"] = error
            elif status in ("completed", "no_safe_menu", "no_feasible_menu",
                            "needs_clarification"):
                # 成功/业务终态：清除之前残留的错误（避免"completed + 旧错误码"不一致）
                self._requests[request_id]["error"] = None
            self._persist_request(request_id)

    def _trigger_workflow(self, request_id: str, session_id: str, body: dict) -> None:
        """在后台线程中触发 C3 工作流。"""
        import threading

        def _run():
            try:
                from food_agent_v2.c3.runner import WorkflowRunner
                runner = WorkflowRunner()
                runner.run(
                    request_id=request_id,
                    session_id=session_id,
                    message=body.get("message", ""),
                    participants=body.get("participants", []),
                    config=body.get("config"),
                )
            except Exception as e:
                self.update_status(request_id, "failed",
                                  error={"code": "WORKFLOW_ERROR", "message": str(e)})
                self._emit_event(request_id, SSEEventType.ERROR, {
                    "error_code": "WORKFLOW_ERROR",
                    "message": str(e)[:500],
                })

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    @staticmethod
    def _hash_payload(body: dict) -> str:
        """规范化载荷并计算哈希。"""
        import hashlib
        normalized = json.dumps(body, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# 全局单例
api = RecommendationAPI()
