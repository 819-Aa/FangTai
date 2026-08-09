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
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from food_agent_v2.core.config import load_config, AppConfig
from food_agent_v2.d1 import api
from food_agent_v2.d1.schemas import validate_create_request


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
    body = await request.json()
    code, resp = api.create_request(body)
    if code >= 400:
        raise HTTPException(status_code=code, detail=resp)
    return resp


@app.get("/v1/recommendation-requests/{request_id}")
async def get_recommendation_status(request_id: str):
    code, resp = api.get_request_status(request_id)
    if code == 404:
        raise HTTPException(status_code=404, detail=resp)
    return resp


@app.get("/v1/recommendation-requests/{request_id}/events")
async def stream_events(request_id: str, request: Request):
    last_event_id = request.headers.get("Last-Event-ID")

    async def event_generator():
        sent = 0
        while True:
            events = api.subscribe_events(request_id, last_event_id)
            for event in events:
                eid = event.get("id", "")
                if last_event_id and int(eid) <= int(last_event_id):
                    continue
                yield f"id: {eid}\nevent: {event['event']}\ndata: {event['data']}\n\n"
                sent += 1
                last_event_id = eid

            if sent == 0:
                # 发送心跳
                yield f": heartbeat\n\n"
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
        raise HTTPException(status_code=code, detail=resp)
    return resp


# ---- 用户档案（供前端选择参与者） ----

@app.get("/v1/users")
async def list_users():
    import json as _json
    from food_agent_v2.core.paths import CLEANED_USERS
    users = []
    with CLEANED_USERS.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                u = _json.loads(line)
                users.append({
                    "id": u["user_id"],
                    "display_name": f"用户{u['user_id']}",
                    "age": u.get("age"),
                    "gender": u.get("gender"),
                    "allergies": u.get("allergies", []),
                    "diseases": u.get("diseases", []),
                    "special_group": u.get("special_group", []),
                })
    return {"items": users, "total": len(users)}


# ---- 会话 ----

@app.post("/v1/sessions")
async def create_session(request: Request):
    await request.json()  # consume body but we generate the session
    import uuid
    sid = f"sess_{uuid.uuid4().hex[:8]}"
    return {"session_id": sid, "created_at": time.time()}


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str):
    return {
        "session_id": session_id,
        "participant_refs": [],
        "request_count": 0,
        "last_request_at": None,
        "current_menu": None,
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
