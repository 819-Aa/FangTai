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
    yield
    # 关闭
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

    async def event_generator():
        # 局部绑定，修复 UnboundLocalError；稳定字符串 event_id 精确续传（不 int() 比较）
        last_event_id = initial_last_event_id
        while True:
            events = api.subscribe_events(request_id, last_event_id)
            yielded_any = False
            for event in events:
                eid = event.get("id", "")
                yield f"id: {eid}\nevent: {event['event']}\ndata: {event['data']}\n\n"
                yielded_any = True
                last_event_id = eid

            if not yielded_any:
                # 无新事件时发送心跳
                yield ": heartbeat\n\n"
            await asyncio.sleep(15)

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


# ---- 会话（连接 C4 创建/恢复边界；非假实现） ----

def _persist_session_row(session_id: str, participant_refs: list[str]) -> None:
    """将会话边界写入 MySQL（INSERT IGNORE），使 GET /sessions/{id} 返回真实数据。"""
    import json as _json

    import pymysql

    from food_agent_v2.core.config import load_config
    cfg = load_config().mysql
    conn = pymysql.connect(host=cfg.host, port=cfg.port,
                           user=cfg.user, password=cfg.password,
                           database=cfg.database, charset="utf8mb4")
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT IGNORE INTO sessions "
            "(session_id, participant_refs, request_count) VALUES (%s, %s, 0)",
            (session_id, _json.dumps(participant_refs, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


@app.post("/v1/sessions")
async def create_session(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    participant_refs = [p.get("participant_ref") for p in body.get("participants", [])
                        if isinstance(p, dict) and p.get("participant_ref")]
    from food_agent_v2.c4 import ContextService
    sid = ContextService().create_session(participant_refs, {})
    _persist_session_row(sid, participant_refs)
    return {"session_id": sid, "created_at": time.time()}


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str):
    from food_agent_v2.c4.mysql_repository import MySQLSessionMemorySource
    src = MySQLSessionMemorySource()
    try:
        meta = src.load_session(session_id)
        menus = src.load_menu_versions(session_id)
    except Exception:
        meta, menus = None, []
    if not meta:
        return JSONResponse(status_code=404, content={
            "error": "NOT_FOUND", "message": "session not found"})
    return {
        "session_id": session_id,
        "participant_refs": meta.get("participant_refs", []),
        "request_count": meta.get("request_count", 0),
        "current_menu": menus[-1] if menus else None,
    }


# ---- 健康检查 ----

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


def main():
    import uvicorn
    cfg = get_config()
    uvicorn.run(app, host=cfg.api.host, port=cfg.api.port)


if __name__ == "__main__":
    main()
