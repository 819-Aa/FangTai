"""D1 API 与 SSE —— 对外 HTTP 接口适配层。

将 HTTP 请求适配为 C3 工作流调用，SSE 事件序列化为客户端可消费的 JSON。
不包含业务逻辑——只是 REST 到 C3 的翻译层。
"""

from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar

from food_agent_v2.c4 import ContextService
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


_active_execution: ContextVar[dict | None] = ContextVar("d1_active_execution", default=None)


class RecommendationAPI:
    """推荐 API 端点处理。"""

    EXECUTION_MAX_SECONDS = 600

    def __init__(self):
        self._requests: dict[str, dict] = {}             # request_id → request state
        self._idempotency: dict[str, dict] = {}          # idempotency_key → {payload_hash, request_id}
        self._events: dict[str, list[dict]] = {}         # request_id → SSE events
        self._event_cursors: dict[str, int] = {}          # request_id → next event_id

    # ---- Redis 持久化（API 重启后请求状态/SSE/幂等不丢；文档 §20：Redis 管运行时） ----

    def _persist_request(self, request_id: str, strict: bool = False) -> bool:
        if request_id not in self._requests:
            return False
        if not self._can_project(request_id):
            return False
        from food_agent_v2.c4.redis_store import RedisSessionStore
        try:
            blob = {
                "state": self._requests[request_id],
                "events": self._events.get(request_id, []),
                "cursor": self._event_cursors.get(request_id, 1),
            }
            generation = self._requests[request_id].get("execution_generation")
            if generation is None:
                RedisSessionStore().save_request(request_id, blob)
            else:
                return RedisSessionStore().save_request_fenced(request_id, blob, generation)
            return True
        except Exception:
            if strict:
                raise
            return generation is None  # legacy Redis 不可用时降级为内存

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

    # ---- POST /v1/recommendation-requests ----

    def create_request(self, body: dict) -> tuple[int, dict]:
        """创建推荐请求。返回 (status_code, response_body)。

        幂等声明为原子操作：
        通过 MySQL request_acceptances 原子声明与去重，Redis 保存运行态缓存。
        """
        # 校验
        error = validate_create_request(body)
        if error:
            return 422, error

        # 匿名 participant_ref → 内部 user_id 映射
        enhanced, map_errors = resolve_participants(
            body.get("participants", []))
        if map_errors:
            return 422, {"error": "VALIDATION_FAILED", "details": map_errors}

        key = body["idempotency_key"]
        payload_hash = self._hash_payload(body)
        session_id = body.get("session_id")

        import hashlib
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()

        try:
            c4 = ContextService()
        except Exception as exc:
            return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc)}

        # Step 3: 先查 MySQL request_acceptances（同键去重，绝不创建孤立空会话）
        try:
            acceptance = c4.load_request_acceptance(key_hash)
        except Exception as exc:
            return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc)}

        if isinstance(acceptance, dict):
            return self._resolve_v2_existing(acceptance, body, enhanced, c4)

        # 检查是否已有 session_id，若有则必须为 v2/langgraph 协议
        if session_id:
            try:
                clar_state = c4.load_clarification_state(session_id)
            except Exception as exc:
                return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc)}

            if isinstance(clar_state, dict):
                if (
                    clar_state.get("protocol_version") in ("v1", "legacy")
                    or clar_state.get("workflow_mode") == "fast_path"
                    or (clar_state.get("protocol_version") and clar_state.get("protocol_version") != "v2")
                    or (clar_state.get("workflow_mode") and clar_state.get("workflow_mode") != "langgraph")
                ):
                    return 409, {
                        "error": "SESSION_PROTOCOL_UNSUPPORTED",
                        "message": "该会话协议为旧版本，不再支持推荐生成，请创建新会话",
                    }
            else:
                return 404, {
                    "error": "SESSION_NOT_FOUND",
                    "message": "会话不存在，请创建新会话",
                }
            claim_session_id = session_id
        else:
            claim_session_id = f"sess_{uuid.uuid4().hex[:8]}"
            try:
                c4.ensure_session_record(
                    claim_session_id,
                    [p["participant_ref"] for p in enhanced],
                    workflow_mode="langgraph",
                )
            except Exception as exc:
                return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc)}

        # 未命中 request_acceptances，才做 MySQL active/过期预检与自然语言选项识别
        clar_resp = body.get("clarification_response")
        derived_clar_resp = None
        if not clar_resp and claim_session_id:
            # 尝试对单活跃问题的自然语言打字进行智能补齐（Human-Friendly 松绑容错）
            try:
                active_q_cand = c4.load_active_clarification(claim_session_id)
                if active_q_cand and active_q_cand.get("status") == "pending":
                    opts = (active_q_cand.get("public_payload") or {}).get("options") or []
                    if opts:
                        from food_agent_v2.c3.graph_orchestrator import (
                            LangGraphRecommendationOrchestrator,
                        )
                        msg_text = body.get("message", "")
                        matched_opt, match_st = (
                            LangGraphRecommendationOrchestrator._resolve_option_selection(
                                msg_text, opts
                            )
                        )
                        if match_st == "matched" and matched_opt:
                            opt_id = matched_opt.get("option_id")
                            if opt_id is not None:
                                derived_clar_resp = {
                                    "question_id": active_q_cand["question_id"],
                                    "option_id": int(opt_id),
                                }
                                # 保持原始 body 只读，防止 hash 漂移
            except Exception:
                pass

        effective_clar_resp = clar_resp or derived_clar_resp
        if effective_clar_resp and claim_session_id:
            qid = effective_clar_resp.get("question_id")
            import time
            try:
                if c4.is_clarification_committed(claim_session_id, qid):
                    return 409, {
                        "error": "CLARIFICATION_ALREADY_APPLIED",
                        "message": "该澄清选项已在先前的请求中提交",
                    }
            except Exception as exc:
                return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc)}

            try:
                active_q = c4.load_active_clarification(claim_session_id)
            except Exception as exc:
                return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc)}

            if active_q is None:
                return 409, {
                    "error": "CLARIFICATION_STALE",
                    "message": "所选澄清问题已失效或已被替代",
                }
            if active_q.get("question_id") != qid:
                return 409, {
                    "error": "CLARIFICATION_STALE",
                    "message": f"当前活跃澄清问题为 {active_q.get('question_id')}，但请求指定了 {qid}",
                }
            exp = active_q.get("expires_at")
            if exp and time.time() > exp:
                return 409, {
                    "error": "CLARIFICATION_EXPIRED",
                    "message": "澄清选项已过期",
                }

        # Redis 幂等可用性校验（如 Redis 存储不可用或测试故障注入）
        try:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            claim_check = RedisSessionStore().claim_idempotency(
                key, payload_hash, "", "", "", ""
            )
            if claim_check and claim_check[0] == "unavailable":
                return 503, {
                    "error": "IDEMPOTENCY_STORE_UNAVAILABLE",
                    "message": "idempotency store unavailable",
                }
        except Exception:
            pass

        # 预检通过，原子声明 request_acceptances
        request_id = str(uuid.uuid4())
        try:
            claim_state, claimed_rec = c4.claim_request_acceptance(
                key_hash, payload_hash, request_id, claim_session_id
            )
        except Exception as exc:
            return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc)}

        if claim_state == "winner":
            now = now_iso()
            execution_owner = uuid.uuid4().hex
            try:
                execution_claim = c4.claim_request_execution_v2(
                    request_id, execution_owner, 60,
                )
                if not execution_claim:
                    return 503, {"error": "REQUEST_RECOVERY_PENDING", "request_id": request_id}
            except Exception as exc:
                return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc),
                             "request_id": request_id}

            execution_body = body
            if derived_clar_resp and not body.get("clarification_response"):
                execution_body = dict(body, clarification_response=derived_clar_resp)

            try:
                return self._create_new(request_id, key, payload_hash, execution_body,
                                        enhanced, claim_session_id, now,
                                        execution_owner=execution_owner,
                                        execution_generation=execution_claim["generation"])
            except Exception:
                # 接受记录已持久化：释放本代次供同键原始 POST 安全重领，
                # 绝不退回不带 owner/generation 的旧执行入口。
                try:
                    c4.finish_request_execution_v2(
                        request_id, execution_owner, execution_claim["generation"], False,
                    )
                except Exception:
                    pass  # DB 租约仍会到期，客户端可用原始 POST 重试
                return 503, {
                    "error": "REQUEST_RECOVERY_PENDING",
                    "request_id": request_id,
                    "message": "请求启动未完成，请使用相同幂等键与载荷重试",
                }
        elif claim_state == "existing" and claimed_rec:
            return self._resolve_v2_existing(claimed_rec, body, enhanced, c4)
        else:
            return 503, {
                "error": "IDEMPOTENCY_STORE_UNAVAILABLE",
                "message": "idempotency store unavailable",
            }

    def _resolve_v2_existing(self, existing: dict, body: dict,
                             enhanced: list[dict], c4: ContextService) -> tuple[int, dict]:
        """Recover a v2 acceptance with the retried payload under a single DB lease."""
        payload_hash = self._hash_payload(body)
        if existing.get("payload_hash") != payload_hash:
            return 409, {"error": "IDEMPOTENCY_KEY_REUSED",
                         "existing_request_id": existing.get("request_id")}
        request_id = existing["request_id"]
        session_id = existing["session_id"]
        try:
            log = c4.load_recommendation_log(request_id)
        except Exception as exc:
            return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc),
                         "request_id": request_id, "session_id": session_id}
        if log:
            return self.get_request_status(request_id)

        execution_owner = uuid.uuid4().hex
        try:
            claimed = c4.claim_request_execution_v2(request_id, execution_owner, 60)
        except Exception as exc:
            return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc),
                         "request_id": request_id, "session_id": session_id}
        if claimed:
            self._restore_request(request_id)
            existing_req = self._requests.get(request_id)
            execution_body = body
            if existing_req and existing_req.get("clarification_response") and not body.get("clarification_response"):
                execution_body = dict(body, clarification_response=existing_req["clarification_response"])
            try:
                return self._create_new(
                    request_id, body["idempotency_key"], payload_hash, execution_body,
                    enhanced, existing["session_id"], now_iso(),
                    execution_owner=execution_owner,
                    execution_generation=claimed["generation"],
                )
            except Exception:
                try:
                    c4.finish_request_execution_v2(
                        request_id, execution_owner, claimed["generation"], False,
                    )
                except Exception:
                    pass
                return 503, {
                    "error": "REQUEST_RECOVERY_PENDING",
                    "request_id": request_id,
                    "session_id": session_id,
                    "message": "请求启动未完成，请使用相同幂等键与载荷重试",
                }
        self._restore_request(request_id)
        req = self._requests.get(request_id)
        try:
            current_claim = c4.load_request_acceptance_by_request_id(request_id)
        except Exception as exc:
            return 503, {"error": "DATABASE_UNAVAILABLE", "message": str(exc),
                         "request_id": request_id, "session_id": session_id}
        if (req and req.get("status") in ("accepted", "running", "needs_clarification", "completed")
                and self._cache_matches_acceptance(req, current_claim)):
            return self._resolve_existing(existing, payload_hash)
        return 503, {"error": "REQUEST_RECOVERY_PENDING", "request_id": request_id,
                     "session_id": session_id,
                     "message": "request execution lease is active; retry later"}

    def _resolve_existing(self, existing: dict,
                          payload_hash: str) -> tuple[int, dict]:
        """按幂等语义解析既有记录：同载荷 200 原 request_id，不同载荷 409。

        优先返回完整 request state（若赢家已写完）；否则从 MySQL recommendation_logs 恢复事实；
        若均无则直接用原子记录中的公开元数据构造 200 响应。
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
        # Redis / 内存中没有，尝试从 MySQL recommendation_logs 恢复事实
        log = None
        if rid:
            try:
                c4 = ContextService()
                log = c4.load_recommendation_log(rid)
            except Exception:
                pass
        if log:
            res_sum = strip_forbidden_fields(req.get("result_summary")) if (req and req.get("result_summary")) else None
            if not res_sum and log.get("status") == "completed" and log.get("health_evidence"):
                ev_data = log["health_evidence"]
                if isinstance(ev_data, dict) and ("menu_items" in ev_data or "recipe_ids" in ev_data):
                    res_sum = strip_forbidden_fields({
                        "menu_summary": {
                            "build_id": ev_data.get("build_id", ""),
                            "plan_id": log.get("final_plan_id", ""),
                            "menu_hash": ev_data.get("menu_hash", ""),
                            "recipe_ids": ev_data.get("recipe_ids", []),
                            "items": ev_data.get("menu_items", []),
                        }
                    })
            return 200, {
                "request_id": log["request_id"],
                "session_id": log.get("session_id", existing.get("session_id", "")),
                "status": log.get("status", "accepted"),
                "created_at": str(log.get("created_at") or existing.get("created_at", "")),
                "result_summary": res_sum,
            }
        # 赢家尚未写完 request state → 原子记录公开元数据
        return 200, {
            "request_id": existing.get("request_id"),
            "session_id": existing.get("session_id", ""),
            "status": existing.get("status", "accepted"),
            "created_at": str(existing.get("created_at", "")),
            "result_summary": None,
        }

    def _create_new(self, request_id: str, key: str, payload_hash: str,
                    body: dict, enhanced: list[dict],
                    session_id: str, now: str,
                    execution_owner: str | None = None,
                    execution_generation: int | None = None) -> tuple[int, dict]:
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
            "clarification_response": body.get("clarification_response"),
            "config": body.get("config", {}),
            "stage_events_cursor": 0,
            "result_summary": None,
            "error": None,
        }
        if execution_generation is not None:
            req_state["execution_generation"] = execution_generation
            req_state["execution_owner"] = execution_owner

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
        if execution_owner:
            try:
                self._trigger_workflow(
                    request_id, session_id, body,
                    execution_owner=execution_owner,
                    execution_generation=execution_generation,
                )
            except TypeError:
                self._trigger_workflow(request_id, session_id, body)
        else:
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

        # MySQL 是已提交的权威事实
        log = None
        c4 = None
        try:
            c4 = ContextService()
            log = c4.load_recommendation_log(request_id)
            acceptance = c4.load_request_acceptance_by_request_id(request_id)
        except Exception as exc:
            return 503, {"error": "DATABASE_ERROR", "message": str(exc)}

        if not self._cache_matches_acceptance(req, acceptance):
            req = None

        if log:
            created_iso = str(log.get("created_at") or (req["created_at"] if req else now_iso()))
            sess_id = log.get("session_id") or (req.get("session_id") if req else "")
            res_sum = strip_forbidden_fields(req.get("result_summary")) if (req and req.get("result_summary")) else None
            if not res_sum and log.get("status") == "completed" and log.get("health_evidence"):
                ev_data = log["health_evidence"]
                if isinstance(ev_data, dict) and ("menu_items" in ev_data or "recipe_ids" in ev_data):
                    res_sum = strip_forbidden_fields({
                        "menu_summary": {
                            "build_id": ev_data.get("build_id", ""),
                            "plan_id": log.get("final_plan_id", ""),
                            "menu_hash": ev_data.get("menu_hash", ""),
                            "recipe_ids": ev_data.get("recipe_ids", []),
                            "items": ev_data.get("menu_items", []),
                        }
                    })
            resp = {
                "request_id": log["request_id"],
                "session_id": sess_id,
                "status": log["status"],
                "created_at": created_iso,
                "updated_at": created_iso,
                "stage_events_cursor": req.get("stage_events_cursor", 0) if req else 0,
                "result_summary": res_sum,
                "error": strip_forbidden_fields(req.get("error")) if req else None,
            }
            if log["status"] == "needs_clarification" and sess_id and c4:
                try:
                    active_q = c4.load_produced_active_clarification(request_id)
                    if active_q:
                        pub = active_q.get("public_payload") or {}
                        opts = pub.get("options") or []
                        resp["active_clarification"] = {
                            "question_id": active_q.get("question_id"),
                            "question_text": pub.get("question_text", ""),
                            "options": [
                                {"option_id": o["option_id"], "text": o.get("text", "")}
                                for o in opts if isinstance(o, dict)
                            ],
                            "expires_at": active_q.get("expires_at"),
                            "status": active_q.get("status", "pending"),
                        }
                except Exception as exc:
                    return 503, {"error": "DATABASE_ERROR", "message": str(exc)}
            return 200, resp

        if not req:
            # Redis 丢失且 MySQL 无终态日志：检查是否为已知已接受请求
            if c4:
                try:
                    acceptance = c4.load_request_acceptance_by_request_id(request_id)
                    if acceptance:
                        return 503, {"error": "REQUEST_STATE_UNAVAILABLE",
                                     "request_id": request_id,
                                     "message": "request state unavailable; retry the original POST"}
                except Exception as exc:
                    return 503, {"error": "DATABASE_ERROR", "message": str(exc)}
            return 404, {"error": "NOT_FOUND", "message": "request not found"}

        if isinstance(acceptance, dict) and acceptance.get("execution_generation", 0) > 0:
            if acceptance.get("status") == "recovery_required":
                return 200, {
                    "request_id": request_id,
                    "session_id": req["session_id"],
                    "status": "recovery_required",
                    "created_at": req["created_at"],
                    "updated_at": req.get("updated_at", req["created_at"]),
                    "stage_events_cursor": 0,
                    "result_summary": None,
                    "error": None,
                }
            if (acceptance.get("status") != "running"
                    or (acceptance.get("execution_lease_until") is not None
                        and acceptance["execution_lease_until"] <= time.time())):
                return 503, {"error": "REQUEST_STATE_UNAVAILABLE",
                             "request_id": request_id}

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

    def subscribe_events(self, request_id: str, last_event_id: str | None = None,
                         *, refresh: bool = False) -> list[dict]:
        """获取 SSE 事件（自 last_event_id 精确 id 之后）。

        稳定 SSE event_id 为字符串（如 ev_answer_xxx）；续传按持久化事件列表中的
        精确 event_id 定位并返回其后的事件，绝不强制转换为整数。未知 last_event_id
        返回全部事件（客户端按 event_id 去重）。

        refresh=True 时先从 Redis 重新加载持久事件并合并（跨 worker：其他 worker
        写入的事件不在本 worker 内存，须按稳定 event_id 合并）。
        同时从 MySQL outbox 恢复已投递的权威事件（避免 Redis 丢失或重启丢失）。
        """
        if refresh:
            self._refresh_events(request_id)
        else:
            self._restore_request(request_id)

        # 从 MySQL outbox 恢复已投递事实（仅 status='dispatched'）
        try:
            c4 = ContextService()
            dispatched = c4.load_dispatched_outbox_events(request_id)
            acceptance = c4.load_request_acceptance_by_request_id(request_id)
        except Exception:
            dispatched = []
            acceptance = None

        cached = self._requests.get(request_id)
        if isinstance(acceptance, dict) and acceptance.get("execution_generation", 0) > 0:
            if (not self._cache_matches_acceptance(cached, acceptance)
                    or acceptance.get("status") not in ("running", "terminal")):
                self._events[request_id] = []
        elif cached and cached.get("execution_generation") is not None and acceptance is None:
            return []  # v2 权威代次不可读取时不展示临时事件

        if dispatched:
            current = self._events.get(request_id, [])
            seen_ids = {e.get("id") for e in current if e.get("id")}
            for item in dispatched:
                eid = item.get("event_id")
                if eid and eid not in seen_ids:
                    payload = item.get("payload")
                    if item.get("event_type") == "clarification_needed" and isinstance(payload, dict):
                        payload = {
                            "request_id": request_id,
                            "question_id": payload.get("question_id", ""),
                            "clarification": payload.get("clarification") or payload.get("question_text", ""),
                            "options": [
                                {"option_id": option.get("option_id"), "text": option.get("text", "")}
                                for option in (payload.get("options") or []) if isinstance(option, dict)
                            ],
                            "expires_at": payload.get("expires_at"),
                        }
                    elif item.get("event_type") == "result_committed" and isinstance(payload, dict):
                        payload = {
                            "request_id": request_id,
                            "menu_summary": payload.get("menu_summary") or {},
                        }
                    elif item.get("event_type") == "answer_ready" and isinstance(payload, dict):
                        answer = payload.get("answer") or payload
                        payload = {
                            "text": answer.get("text", ""),
                            "menu_ref": answer.get("menu_ref", ""),
                            "evidence_refs": answer.get("evidence_refs") or [],
                        }
                    if isinstance(payload, dict) and scan_forbidden_fields(payload):
                        continue
                    event_data = (
                        json.dumps(payload, ensure_ascii=False)
                        if isinstance(payload, (dict, list))
                        else str(payload)
                    )
                    current.append({
                        "id": str(eid),
                        "event": item.get("event_type", ""),
                        "data": event_data,
                    })
                    seen_ids.add(eid)
            self._events[request_id] = current

        all_events = self._events.get(request_id, [])
        if not last_event_id:
            return all_events
        for idx, event in enumerate(all_events):
            if event.get("id") == last_event_id:
                return all_events[idx + 1:]
        return all_events

    def _refresh_events(self, request_id: str) -> None:
        """从 Redis 重新加载持久事件，按稳定 event_id 合并到内存（不覆盖本地已有）。"""
        try:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            blob = RedisSessionStore().load_request(request_id)
        except Exception:
            return
        if not blob:
            return
        persisted = blob.get("events", []) or []
        current = self._events.get(request_id, [])
        seen = {e.get("id") for e in current if e.get("id")}
        merged = list(current)
        for e in persisted:
            eid = e.get("id")
            if eid and eid not in seen:
                merged.append(e)
                seen.add(eid)
        self._events[request_id] = merged

    def _emit_event(self, request_id: str, event_type: SSEEventType, payload: dict,
                    event_id: str | None = None) -> None:
        """发布 SSE 事件。

        event_id 为显式稳定 ID（T19 outbox）：相同 event_id 重复发布只保留一份事实，
        且不得再次递增游标。stage_events_cursor 始终为整数，等于已发布事件总数，
        每个首次发布的事件（含稳定字符串 outbox event_id）都递增；重复 event_id
        提前返回不递增。
        """
        if not self._can_project(request_id):
            return
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
        if request_id not in self._requests:
            self._restore_request(request_id)
        previous_request = dict(self._requests[request_id]) if request_id in self._requests else None
        previous_events = list(self._events.get(request_id, []))
        previous_cursor = self._event_cursors.get(request_id)
        if request_id not in self._requests:
            self._requests[request_id] = {
                "request_id": request_id,
                "status": "running",
                "stage_events_cursor": 0,
            }
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
        try:
            if event_id is not None:
                persisted = self._persist_request(request_id, strict=True)
            else:
                persisted = self._persist_request(request_id)
        except Exception:
            persisted = False
            raise
        finally:
            if persisted is False:
                self._events[request_id] = previous_events
                if previous_cursor is None:
                    self._event_cursors.pop(request_id, None)
                else:
                    self._event_cursors[request_id] = previous_cursor
                if previous_request is None:
                    self._requests.pop(request_id, None)
                else:
                    self._requests[request_id] = previous_request
        if persisted is False:
            return
        # P2：事件写入后即时唤醒 SSE 订阅者（Redis Pub/Sub；不可用则回退 15s 轮询）
        self._notify_sse(request_id)

    def _notify_sse(self, request_id: str) -> None:
        """向该 request 的 SSE 订阅者发布即时通知（经 C4 公共接口，不碰 Redis 私有成员）。"""
        try:
            from food_agent_v2.c4.event_notifier import get_event_notifier
            get_event_notifier().publish(request_id)
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
        # 与 state.TERMINAL_STATUSES 一致：needs_clarification / interrupted
        # 也已是终态，不可改写为 cancelled。
        terminal = {
            "completed", "no_safe_menu", "no_feasible_menu",
            "needs_clarification", "failed", "cancelled", "interrupted",
        }
        if req["status"] in terminal:
            return 409, {"error": "REQUEST_ALREADY_TERMINAL", "current_status": req["status"]}

        req["status"] = "cancelled"
        req["updated_at"] = now_iso()
        self._emit_event(request_id, SSEEventType.REQUEST_CANCELLED, {"request_id": request_id})

        # 取消标记：工作流在节点边界检查（TTL 1h）
        try:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            RedisSessionStore().mark_request_cancelled(request_id)
        except Exception:
            pass

        return 200, {
            "request_id": request_id,
            "status": "cancelled",
            "cancelled_at": now_iso(),
        }

    # ---- 工作流集成 ----

    def publish_clarification_event(self, request_id: str,
                                    query_plan: dict | None = None,
                                    *, event_id: str | None = None) -> None:
        """查询理解需要澄清时发布 clarification_needed 事件（文档 09 §8.2）。"""
        qp = query_plan or {}
        raw_options = qp.get("options") or qp.get("clarification_options") or qp.get("inquiry_options") or []
        desensitized_options = []
        for opt in raw_options:
            if isinstance(opt, dict):
                desensitized_options.append({
                    "option_id": opt.get("option_id"),
                    "text": opt.get("text", ""),
                })
            else:
                desensitized_options.append({
                    "option_id": len(desensitized_options) + 1,
                    "text": str(opt),
                })
        payload = {
            "request_id": request_id,
            "question_id": qp.get("question_id") or "",
            "clarification": qp.get(
                "clarification_question") or qp.get("question_text") or "请补充必要信息后再推荐",
            "options": desensitized_options,
            "expires_at": qp.get("expires_at"),
        }
        violations = scan_forbidden_fields(payload)
        if violations:
            self._emit_event(request_id, SSEEventType.ERROR, {
                "error_code": "SENSITIVE_DATA_EXPOSURE",
                "message": f"Forbidden fields: {violations}",
            }, event_id=event_id)
            raise SensitiveDataBlocked(f"SENSITIVE_DATA_EXPOSURE: {violations}")
        self._emit_event(request_id, SSEEventType.CLARIFICATION_NEEDED, payload,
                         event_id=event_id)

    def publish_answer_started(self, request_id: str) -> None:
        """发布 answer_started（不承诺菜单的开场，用于首 Token 计时）。

        只代表请求已开始处理，不代表菜单已生成或校验通过。
        """
        self._emit_event(request_id, SSEEventType.ANSWER_STARTED, {
            "request_id": request_id,
        })

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

        no_safe_menu / no_feasible_menu / failed / interrupted 等终态经此统一发布，
        status 保持各自语义（绝不伪装成普通
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
        if not self._can_project(request_id):
            return
        if request_id in self._requests:
            previous = dict(self._requests[request_id])
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
            if self._persist_request(request_id) is False:
                self._requests[request_id] = previous
                return
            # 幂等记录状态同步（跨重启重试返回最新 status）
            key = self._requests[request_id].get("idempotency_key")
            if key and key in self._idempotency:
                rec = dict(self._idempotency[key])
                rec["status"] = status
                self._idempotency[key] = rec
                self._save_idempotency(key, rec)

    @staticmethod
    def _cache_matches_acceptance(req: dict | None, acceptance: dict | None) -> bool:
        if not isinstance(acceptance, dict):
            return True
        generation = acceptance.get("execution_generation")
        if not isinstance(generation, int) or generation <= 0:
            return True  # legacy request without a v2 execution claim
        return bool(
            req and req.get("execution_owner") == acceptance.get("execution_owner")
            and req.get("execution_generation") == generation
        )

    @staticmethod
    def mark_execution_committed(request_id: str, owner: str, generation: int) -> None:
        """Permit this worker to project the result it just committed atomically."""
        attempt = _active_execution.get()
        if (attempt and attempt["request_id"] == request_id
                and attempt["owner"] == owner
                and attempt["generation"] == generation):
            attempt["committed"] = True

    @staticmethod
    def _can_project(request_id: str) -> bool:
        attempt = _active_execution.get()
        if not attempt or attempt["request_id"] != request_id:
            return True  # API request handler and committed outbox dispatcher
        if attempt.get("committed"):
            return True
        if attempt.get("lost"):
            return False
        if time.monotonic() >= attempt.get("deadline", float("inf")):
            attempt["lost"] = True
            return False
        try:
            held = ContextService().renew_request_execution_v2(
                request_id, attempt["owner"], attempt["generation"], 60,
            )
        except Exception:
            held = False
        if not held:
            attempt["lost"] = True
        return bool(held)

    def _trigger_workflow(self, request_id: str, session_id: str, body: dict,
                          *, execution_owner: str | None = None,
                          execution_generation: int | None = None) -> None:
        """在后台线程中触发 C3 工作流。"""
        import threading

        def _run():
            attempt_token = _active_execution.set({
                "request_id": request_id,
                "owner": execution_owner,
                "generation": execution_generation,
                "committed": False,
                "lost": False,
                "deadline": time.monotonic() + self.EXECUTION_MAX_SECONDS,
            }) if execution_owner is not None else None
            try:
                req = self._requests.get(request_id)
                if not req:
                    return

                # 仅实例化并执行 LangGraphRecommendationOrchestrator（旧编排器不再由 API 启动）
                from food_agent_v2.c3.graph_orchestrator import (
                    LangGraphRecommendationOrchestrator,
                )
                runner = LangGraphRecommendationOrchestrator()
                runner.run(
                    request_id=request_id,
                    session_id=session_id,
                    message=body.get("message", ""),
                    # 内部增强参与者（服务端映射后的 user_id；公共响应不含）
                    participants=req.get("participants", []),
                    config=body.get("config"),
                    clarification_response=req.get("clarification_response"),
                    **({
                        "execution_owner": execution_owner,
                        "execution_generation": execution_generation,
                    } if execution_owner is not None else {}),
                )
            except Exception as e:
                # 总异常兜底：若发现 MySQL 已有终态，不得覆盖成 WORKFLOW_ERROR
                try:
                    c4 = ContextService()
                    existing_log = c4.load_recommendation_log(request_id)
                    if existing_log and existing_log.get("status") in {
                        "completed", "no_safe_menu", "no_feasible_menu",
                        "needs_clarification", "failed", "cancelled", "interrupted",
                    }:
                        return
                except Exception:
                    pass

                self.update_status(request_id, "failed",
                                  error={"code": "WORKFLOW_ERROR", "message": str(e)})
                self._emit_event(request_id, SSEEventType.ERROR, {
                    "error_code": "WORKFLOW_ERROR",
                    "message": str(e)[:500],
                })
            finally:
                if execution_owner:
                    try:
                        context = ContextService()
                        terminal = context.load_recommendation_log(request_id) is not None
                        context.finish_request_execution_v2(
                            request_id, execution_owner, execution_generation, terminal,
                        )
                    except Exception:
                        pass
                if attempt_token is not None:
                    _active_execution.reset(attempt_token)

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
