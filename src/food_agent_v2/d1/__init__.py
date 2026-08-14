"""D1 API 与 SSE —— 对外 HTTP 接口适配层。

将 HTTP 请求适配为 C3 工作流调用，SSE 事件序列化为客户端可消费的 JSON。
不包含业务逻辑——只是 REST 到 C3 的翻译层。
"""

from __future__ import annotations

import json
import uuid

from food_agent_v2.d1.schemas import (
    SSEEventType,
    now_iso,
    resolve_participants,
    scan_forbidden_fields,
    strip_forbidden_fields,
    validate_create_request,
)


class SensitiveDataBlocked(Exception):
    """SSE payload 含禁止字段：阻止该事件发布及后续成功事件（fail-closed）。

    outbox dispatcher 捕获后停止该 request 的后序事件，对应 outbox 记录
    不会被当作成功投递。
    """


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
        # 幂等键恢复（含完整公开元数据，保证幂等判定与 200 响应完整）
        key = blob.get("state", {}).get("idempotency_key")
        rid = blob.get("state", {}).get("request_id")
        phash = blob.get("state", {}).get("payload_hash")
        if key and rid:
            self._idempotency[key] = {
                "payload_hash": phash,
                "request_id": rid,
                "session_id": blob.get("state", {}).get("session_id"),
                "created_at": blob.get("state", {}).get("created_at"),
                "status": blob.get("state", {}).get("status"),
            }

    def _save_idempotency(self, key: str, record: dict) -> None:
        """幂等记录持久化到 Redis（含公开响应元数据；跨 API 重启有效）。"""
        try:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            RedisSessionStore().save_idempotency(key, record)
        except Exception:
            pass

    def _load_idempotency(self, key: str) -> dict | None:
        """从 Redis 加载幂等索引（跨 API 重启）。Redis 不可用返回 None。"""
        try:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            return RedisSessionStore().load_idempotency(key)
        except Exception:
            return None

    # ---- POST /v1/recommendation-requests ----

    def create_request(self, body: dict) -> tuple[int, dict]:
        """创建推荐请求。返回 (status_code, response_body)。

        幂等声明为原子操作（Redis SET NX 写入完整公开元数据）：并发同键同载荷只有
        一个赢家创建/启动工作流（一次 202）；输家即使赢家尚未写完 request state，
        也直接按原子记录返回 200 且同一 request_id；同键不同载荷 409；
        Redis 不可用 → 顶层 503，不写请求、不启动工作流。
        """
        # 校验
        error = validate_create_request(body)
        if error:
            return 422, error

        # 匿名 participant_ref → 内部 user_id 映射（非法/越界/重复/携带 user_id
        # 均在启动工作流前返回 422；映射结果只用于内部传给 C3，不进入公共响应）
        enhanced, map_errors = resolve_participants(
            body.get("participants", []))
        if map_errors:
            return 422, {"error": "VALIDATION_FAILED", "details": map_errors}

        key = body["idempotency_key"]
        payload_hash = self._hash_payload(body)

        # 同实例内存幂等快路径
        existing = self._idempotency.get(key)
        if existing:
            return self._resolve_existing(existing, payload_hash)

        # 原子声明（SET NX 写入完整公开元数据）
        request_id = str(uuid.uuid4())
        session_id = body.get("session_id") or f"sess_{uuid.uuid4().hex[:8]}"
        now = now_iso()
        state, record = self._claim_idempotency(
            key, payload_hash, request_id, session_id, now, "accepted")
        if state == "winner":
            return self._create_new(request_id, key, payload_hash, body,
                                    enhanced, session_id, now)
        if state == "existing" and record:
            # 即使赢家尚未写完 request state，也直接按 record 返回（不误返 409，
            # 不启动第二个工作流）
            self._idempotency[key] = record
            return self._resolve_existing(record, payload_hash)
        # Redis 不可用 → 顶层 503，不写请求、不启动工作流
        return 503, {"error": "IDEMPOTENCY_STORE_UNAVAILABLE",
                     "message": "idempotency store unavailable"}

    def _resolve_existing(self, existing: dict,
                          payload_hash: str) -> tuple[int, dict]:
        """按幂等语义解析既有记录：同载荷 200 原 request_id，不同载荷 409。

        优先返回完整 request state（若赢家已写完）；否则直接用原子记录中的公开
        元数据构造 200 响应。
        """
        if existing.get("payload_hash") != payload_hash:
            return 409, {
                "error": "IDEMPOTENCY_KEY_REUSED",
                "existing_request_id": existing.get("request_id"),
            }
        rid = existing.get("request_id")
        if rid and rid not in self._requests:
            self._restore_request(rid)
        req = self._requests.get(rid) if rid else None
        if req:
            return 200, {
                "request_id": req["request_id"],
                "session_id": req["session_id"],
                "status": req["status"],
                "created_at": req["created_at"],
                "result_summary": strip_forbidden_fields(
                    req.get("result_summary")),
            }
        # 赢家尚未写完 request state → 原子记录公开元数据
        return 200, {
            "request_id": existing.get("request_id"),
            "session_id": existing.get("session_id", ""),
            "status": existing.get("status", "accepted"),
            "created_at": existing.get("created_at", ""),
            "result_summary": None,
        }

    def _claim_idempotency(self, key: str, payload_hash: str,
                           request_id: str, session_id: str,
                           created_at: str, status: str) -> tuple[str, dict | None]:
        """SET NX 原子声明，三态 (winner/existing/unavailable, record)。"""
        try:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            return RedisSessionStore().claim_idempotency(
                key, payload_hash, request_id, session_id, created_at, status)
        except Exception:
            return ("unavailable", None)

    def _create_new(self, request_id: str, key: str, payload_hash: str,
                    body: dict, enhanced: list[dict],
                    session_id: str, now: str) -> tuple[int, dict]:
        """创建请求并启动工作流（仅幂等赢家调用）。

        enhanced 为服务端内部映射后的参与者（含 user_id，只传给 C3）；
        公共响应/SSE 绝不包含 user_id。
        """
        req_state = {
            "request_id": request_id,
            "session_id": session_id,
            "idempotency_key": key,
            "payload_hash": payload_hash,
            "status": "accepted",
            "created_at": now,
            "updated_at": now,
            "participants": enhanced,
            "message": body.get("message", ""),
            "config": body.get("config", {}),
            "stage_events_cursor": 0,
            "result_summary": None,
            "error": None,
        }

        self._requests[request_id] = req_state
        record = {
            "payload_hash": payload_hash,
            "request_id": request_id,
            "session_id": session_id,
            "created_at": now,
            "status": "accepted",
        }
        self._idempotency[key] = record
        self._save_idempotency(key, record)
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
            # 统一禁止字段投影：result_summary/error 不得泄漏 user_id/disease_name 等
            "result_summary": strip_forbidden_fields(req.get("result_summary")),
            "error": strip_forbidden_fields(req.get("error")),
        }

    # ---- GET /v1/recommendation-requests/{request_id}/events ----

    def subscribe_events(self, request_id: str, last_event_id: str | None = None) -> list[dict]:
        """获取 SSE 事件（自 last_event_id 精确 id 之后）。

        稳定 SSE event_id 为字符串（如 ev_answer_xxx）；续传按持久化事件列表中的
        精确 event_id 定位并返回其后的事件，绝不强制转换为整数。未知 last_event_id
        返回全部事件（客户端按 event_id 去重）。
        """
        self._restore_request(request_id)
        all_events = self._events.get(request_id, [])
        if not last_event_id:
            return all_events
        for idx, event in enumerate(all_events):
            if event.get("id") == last_event_id:
                return all_events[idx + 1:]
        return all_events

    def _emit_event(self, request_id: str, event_type: SSEEventType, payload: dict,
                    event_id: str | None = None) -> None:
        """发布 SSE 事件。

        event_id 为显式稳定 ID（T19 outbox）：相同 event_id 重复发布只保留一份事实，
        且不得再次递增游标。stage_events_cursor 始终为整数，等于已发布事件总数，
        每个首次发布的事件（含稳定字符串 outbox event_id）都递增；重复 event_id
        提前返回不递增。
        """
        if event_id is not None:
            existing = [e for e in self._events.get(request_id, [])
                        if e.get("id") == event_id]
            if existing:
                return  # 幂等：同 event_id 已发布 → 不重复、不递增游标
            eid = event_id
        else:
            eid = str(self._event_cursors.get(request_id, 1))
            while any(e.get("id") == eid for e in self._events.get(request_id, [])):
                self._event_cursors[request_id] = int(eid) + 1
                eid = str(self._event_cursors[request_id])
        event = {
            "id": eid,
            "event": event_type.value,
            "data": json.dumps(payload, ensure_ascii=False),
        }
        self._events.setdefault(request_id, []).append(event)
        published = len(self._events[request_id])
        self._event_cursors[request_id] = published + 1
        if request_id in self._requests:
            self._requests[request_id]["stage_events_cursor"] = published
        self._persist_request(request_id)
        # P2：事件写入后即时唤醒 SSE 订阅者（Redis Pub/Sub；不可用则回退 15s 轮询）
        self._notify_sse(request_id)

    def _notify_sse(self, request_id: str) -> None:
        """向该 request 的 SSE 订阅者发布即时通知（Redis Pub/Sub）。

        单进程与多进程共用同一 Redis 通道；Redis 不可用时静默（SSE 回退 15s 轮询）。
        """
        try:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            store = RedisSessionStore()
            store._connect()
            if store._client is not None:
                store._client.publish(store._key("sse", request_id), "1")
        except Exception:
            pass

    # ---- POST /v1/recommendation-requests/{request_id}/cancel ----

    def cancel_request(self, request_id: str) -> tuple[int, dict]:
        """取消请求。写 Redis 取消标记，工作流线程在节点边界检查并停止（文档 §15）。"""
        self._restore_request(request_id)
        req = self._requests.get(request_id)
        if not req:
            return 404, {"error": "NOT_FOUND"}

        # 终态（含 cancelled）不可取消 → 重复取消返回 409。
        # 与 state.TERMINAL_STATUSES 一致：needs_clarification /
        # strict_time_indeterminate / interrupted 也已是终态，不可改写为 cancelled。
        terminal = {
            "completed", "no_safe_menu", "no_feasible_menu",
            "needs_clarification", "strict_time_indeterminate",
            "failed", "cancelled", "interrupted",
        }
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
        payload = {
            "request_id": request_id,
            "clarification": (query_plan or {}).get(
                "clarification_question", "请补充必要信息后再推荐"),
        }
        if scan_forbidden_fields(payload):
            self._emit_event(request_id, SSEEventType.ERROR, {
                "error_code": "SENSITIVE_DATA_EXPOSURE",
                "message": f"Forbidden fields: {scan_forbidden_fields(payload)}",
            })
            return
        self._emit_event(request_id, SSEEventType.CLARIFICATION_NEEDED, payload)

    def publish_analysis_event(self, request_id: str, stage: str,
                               summary: str, evidence_refs: list[str]) -> None:
        """C3 通过此接口发布阶段分析事件（payload 为原始 stage/summary/evidence_refs）。"""
        payload = {
            "stage": stage,
            "summary": summary,
            "evidence_refs": evidence_refs,
        }
        if scan_forbidden_fields(payload):
            return  # 禁止字段拦截：不发布
        self._emit_event(request_id, SSEEventType.ANALYSIS_READY, payload)

    def publish_answer_event(self, request_id: str, text: str,
                            menu_ref: str, evidence_refs: list[str],
                            event_id: str | None = None) -> None:
        """C3 通过此接口发布回答事件（event_id 为 outbox 稳定 ID，幂等去重）。

        禁止字段拦截时：发 error 事件并抛 SensitiveDataBlocked → outbox dispatcher
        停止该 request 后序事件，result_committed 不再发布，对应 outbox 记录
        不被当作成功投递。
        """
        payload = {
            "text": text,
            "menu_ref": menu_ref,
            "evidence_refs": evidence_refs,
        }
        violations = scan_forbidden_fields(payload)
        if violations:
            self._emit_event(request_id, SSEEventType.ERROR, {
                "error_code": "SENSITIVE_DATA_EXPOSURE",
                "message": f"Forbidden fields: {violations}",
            }, event_id=event_id)
            raise SensitiveDataBlocked(
                f"SENSITIVE_DATA_EXPOSURE: {violations}")
        self._emit_event(request_id, SSEEventType.ANSWER_READY, payload,
                         event_id=event_id)

    def publish_terminal(self, request_id: str, status: str,
                         message: str | None = None) -> None:
        """发布业务终态通知（request_terminal）。

        no_safe_menu / no_feasible_menu / strict_time_indeterminate / failed /
        interrupted 等终态经此统一发布，status 保持各自语义（绝不伪装成普通
        failed）；payload 经禁止字段投影，绝不泄漏 user_id/健康详情。
        """
        payload = strip_forbidden_fields({
            "request_id": request_id,
            "status": status,
            "message": message or "",
        })
        self._emit_event(request_id, SSEEventType.REQUEST_TERMINAL, payload)

    def publish_result_committed(self, request_id: str, menu_summary: dict,
                                 event_id: str | None = None) -> None:
        """发布 result_committed（禁止字段扫描；被拦截时抛错阻止投递）。"""
        payload = {
            "request_id": request_id,
            "menu_summary": menu_summary,
        }
        violations = scan_forbidden_fields(payload)
        if violations:
            self._emit_event(request_id, SSEEventType.ERROR, {
                "error_code": "SENSITIVE_DATA_EXPOSURE",
                "message": f"Forbidden fields: {violations}",
            }, event_id=event_id)
            raise SensitiveDataBlocked(
                f"SENSITIVE_DATA_EXPOSURE: {violations}")
        self._emit_event(request_id, SSEEventType.RESULT_COMMITTED, payload,
                         event_id=event_id)

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
            # 幂等记录状态同步（跨重启重试返回最新 status）
            key = self._requests[request_id].get("idempotency_key")
            if key and key in self._idempotency:
                rec = dict(self._idempotency[key])
                rec["status"] = status
                self._idempotency[key] = rec
                self._save_idempotency(key, rec)

    def _trigger_workflow(self, request_id: str, session_id: str, body: dict) -> None:
        """在后台线程中触发 C3 工作流。"""
        import threading

        def _run():
            try:
                from food_agent_v2.c3.runner import WorkflowRunner
                req = self._requests.get(request_id)
                if not req:
                    return
                runner = WorkflowRunner()
                runner.run(
                    request_id=request_id,
                    session_id=session_id,
                    message=body.get("message", ""),
                    # 内部增强参与者（服务端映射后的 user_id；公共响应不含）
                    participants=req.get("participants", []),
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
