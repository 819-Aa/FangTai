"""V2 FastAPI 应用入口。

启动流程：加载配置 → 预热模型（可选）→ 启动 HTTP。
POST /v1/recommendation-requests
GET  /v1/recommendation-requests/{request_id}
GET  /v1/recommendation-requests/{request_id}/events
POST /v1/recommendation-requests/{request_id}/cancel
POST /v1/sessions
GET  /v1/sessions/{session_id}
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from food_agent_v2.core.config import AppConfig, load_config
from food_agent_v2.d1 import api

_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动
    cfg = get_config()
    print(f"[V2] Starting on {cfg.api.host}:{cfg.api.port}")
    if cfg.models.rag_warmup_on_startup:
        # 预热 BGE-M3 + BGE-Reranker（文档 07 §13），首轮请求不承担冷加载
        try:
            from food_agent_v2.c1 import warmup_models
            print("[V2] Model warmup: loading BGE-M3 + Reranker...")
            warmup_models()
            print("[V2] Model warmup: done")
        except Exception as e:
            print(f"[V2] Model warmup failed (continuing): {e}")

    # P2：Qdrant 预热——连接 + 一次不改变数据的最小检索，避免首请求承担连接与
    # 客户端导入成本（设计文档 §5.6）。
    try:
        from food_agent_v2.c1 import get_retrieval_service
        print("[V2] Qdrant warmup: connecting + minimal retrieve...")
        get_retrieval_service().retrieve("预热", top_k=5)
        print("[V2] Qdrant warmup: done")
    except Exception as e:
        print(f"[V2] Qdrant warmup failed (continuing): {e}")

    # ADR-0006：outbox 恢复循环——启动补投崩溃遗留的 pending 事件，
    # 后台周期扫描兜底首次投递失败；成功 SSE 不得永久丢失。
    stop_event = threading.Event()

    def _outbox_recovery_loop() -> None:
        from food_agent_v2.application.outbox import dispatch_pending
        # 首次立即补投
        try:
            n = dispatch_pending()
            if n:
                print(f"[V2] Outbox recovery: dispatched {n} pending events")
        except Exception as e:  # noqa: BLE001 —— MySQL 未就绪时静默，下轮重试
            print(f"[V2] Outbox recovery failed (retrying): {e}")
        # 周期扫描（每 30s 兜底）
        while not stop_event.wait(30):
            try:
                n = dispatch_pending()
                if n:
                    print(f"[V2] Outbox recovery: dispatched {n} pending events")
            except Exception:
                pass  # 下一轮重试

    recovery_thread = threading.Thread(
        target=_outbox_recovery_loop, daemon=True, name="outbox-recovery")
    recovery_thread.start()

    yield
    # 关闭
    stop_event.set()
    # P2：关闭 SiliconFlow 进程级 httpx 客户端（释放连接池）
    try:
        from food_agent_v2.c1.siliconflow import close_siliconflow_http_client
        close_siliconflow_http_client()
    except Exception:
        pass
    print("[V2] Shutting down")


app = FastAPI(title="Food Agent V2", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_config().api.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- 推荐请求 ----

def _require_object_body(body) -> dict | None:
    """请求体必须是 JSON 对象；否则返回顶层 422 错误结构。"""
    if not isinstance(body, dict):
        return {"error": "VALIDATION_FAILED",
                "details": [{"field": "body", "issue": "must be a JSON object"}]}
    return None


@app.post("/v1/recommendation-requests")
async def create_recommendation(request: Request):
    try:
        body = await request.json()
    except Exception:
        # 非法 JSON → 顶层 422 VALIDATION_FAILED（不返回 500）
        return JSONResponse(status_code=422, content={
            "error": "VALIDATION_FAILED",
            "details": [{"field": "body", "issue": "invalid JSON"}],
        })
    err = _require_object_body(body)
    if err is not None:
        # null/数组/字符串等非对象请求体 → 统一顶层 422
        return JSONResponse(status_code=422, content=err)
    code, resp = api.create_request(body)
    if code >= 400:
        return JSONResponse(status_code=code, content=resp)
    return resp


@app.get("/v1/recommendation-requests/{request_id}")
async def get_recommendation_status(request_id: str):
    code, resp = api.get_request_status(request_id)
    if code == 404:
        return JSONResponse(status_code=404, content=resp)
    return resp


@app.get("/v1/recommendation-requests/{request_id}/events")
async def stream_events(request_id: str, request: Request):
    initial_last_event_id = request.headers.get("Last-Event-ID")

    def _subscribe_notifications():
        """订阅该 request 的即时事件通知（经 C4 公共接口，不碰 Redis 私有成员）。"""
        try:
            from food_agent_v2.c4.event_notifier import get_event_notifier
            return get_event_notifier().subscribe(request_id)
        except Exception:
            return None

    async def event_generator():
        # 局部绑定，修复 UnboundLocalError；稳定字符串 event_id 精确续传（不 int() 比较）
        last_event_id = initial_last_event_id
        subscription = _subscribe_notifications()
        loop = asyncio.get_running_loop()
        try:
            while True:
                # refresh=True：通知后从持久事件源刷新，跨 worker 也不丢事件
                events = api.subscribe_events(request_id, last_event_id, refresh=True)
                yielded_any = False
                for event in events:
                    eid = event.get("id", "")
                    yield f"id: {eid}\nevent: {event['event']}\ndata: {event['data']}\n\n"
                    yielded_any = True
                    last_event_id = eid

                if not yielded_any:
                    # 无新事件时发送心跳
                    yield ": heartbeat\n\n"
                # 等待事件通知（阻塞在 executor 线程，不占事件循环）；
                # 15s 仅作为无事件 heartbeat 兜底，不再是新事件轮询周期。
                if subscription is not None:
                    await loop.run_in_executor(None, subscription.wait, 15.0)
                else:
                    await asyncio.sleep(15)
        finally:
            if subscription is not None:
                try:
                    subscription.close()
                except Exception:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/recommendation-requests/{request_id}/cancel")
async def cancel_recommendation(request_id: str):
    code, resp = api.cancel_request(request_id)
    if code >= 400:
        return JSONResponse(status_code=code, content=resp)
    return resp


# ---- 参与者（公开接口不暴露真实用户档案；前端只生成匿名槽位/participant_ref） ----

@app.get("/v1/users")
async def list_users():
    """公开参与者列表：只返回匿名占位（不读取真实用户档案/健康详情）。

    真实用户依赖仅供内部离线使用，绝不进入公开接口（T20 身份隔离）。
    """
    return {"items": [], "total": 0}


# ---- 会话（只经 C4 公开接口；api_app 不直接依赖 pymysql/MySQL repository） ----

@app.post("/v1/sessions")
async def create_session(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = None
    err = _require_object_body(body)
    if err is not None:
        return JSONResponse(status_code=422, content=err)
    participant_refs = [p.get("participant_ref") for p in body.get("participants", [])
                        if isinstance(p, dict) and p.get("participant_ref")]
    from food_agent_v2.c4 import ContextService
    try:
        sid = ContextService().create_session_record(participant_refs)
    except Exception:
        # 存储基础设施异常 → 顶层 503 固定公开文案，不透传数据库异常
        return JSONResponse(status_code=503, content={
            "error": "SESSION_STORE_UNAVAILABLE",
            "message": "session store unavailable",
        })
    return JSONResponse(status_code=201, content={
        "session_id": sid,
        "created_at": time.time(),
        "last_request_at": None,
    })


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str):
    from food_agent_v2.c4 import ContextService
    try:
        state = ContextService().get_session_state(session_id)
    except Exception as exc:
        # 基础设施异常不得伪装成 404
        return JSONResponse(status_code=503, content={
            "error": "SESSION_STORE_UNAVAILABLE",
            "message": str(exc)[:200],
        })
    if state is None:
        return JSONResponse(status_code=404, content={
            "error": "NOT_FOUND", "message": "session not found"})
    return state


# ---- 健康检查 ----

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/ready")
async def ready():
    from food_agent_v2.application import readiness

    try:
        return readiness.check_readiness()
    except readiness.ServiceNotReady as exc:
        return JSONResponse(status_code=503, content={
            "error": exc.code,
            "message": "service not ready",
            "checks": exc.checks,
        })


def main():
    import uvicorn
    cfg = get_config()
    uvicorn.run(app, host=cfg.api.host, port=cfg.api.port)


if __name__ == "__main__":
    main()
