"""Application 提交服务 —— INV-010：最终结果 + 强制健康审计原子提交到 MySQL。

与文档模块边界一致：Application 提交服务是唯一负责跨模块最终提交的模块。
最终推荐结果、菜单哈希、参与者约束引用、健康证据和请求终态在**同一事务**写入；
任一步失败回滚，抛 `AuditCommitFailed`，不保留成功终态。
"""

from __future__ import annotations

import json

from food_agent_v2.core.config import load_config


class AuditCommitFailed(Exception):
    """INV-010 原子提交失败。"""


def _connect():
    import pymysql
    cfg = load_config()
    return pymysql.connect(
        host=cfg.mysql.host, port=cfg.mysql.port,
        user=cfg.mysql.user, password=cfg.mysql.password,
        database=cfg.mysql.database, charset="utf8mb4",
        autocommit=False,
    )


def commit_request_result(
    request_id: str,
    session_id: str,
    status: str,
    final_plan_id: str,
    health_evidence: dict,
    participant_refs: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    fencing_token: str | None = None,
) -> dict:
    """将请求终态 + 健康证据原子写入 MySQL（INV-010）。

    - 写 recommendation_logs（最终结果 + health_evidence）
    - completed 时写 menu_versions（提交的菜单）
    - 更新 sessions.request_count
    - 写 conversation_events 终态事件
    任一失败回滚，抛 AuditCommitFailed。
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        # 1. 会话（父表，先建）
        cursor.execute(
            "INSERT INTO sessions (session_id, participant_refs, request_count) "
            "VALUES (%s, %s, 1) "
            "ON DUPLICATE KEY UPDATE request_count=request_count+1",
            (session_id, json.dumps(participant_refs or [], ensure_ascii=False)),
        )
        # 2. 最终结果日志（结果 + 健康证据同一事务）；fencing_token 端到端传递不丢弃
        evidence_payload = dict(health_evidence or {})
        if fencing_token is not None:
            evidence_payload["_fencing_token"] = fencing_token
        cursor.execute(
            "INSERT INTO recommendation_logs "
            "(request_id, session_id, status, final_plan_id, health_evidence) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE status=VALUES(status), "
            "final_plan_id=VALUES(final_plan_id), health_evidence=VALUES(health_evidence)",
            (request_id, session_id, status, final_plan_id,
             json.dumps(evidence_payload, ensure_ascii=False)),
        )
        # 3. 完成的菜单版本（依赖 sessions）
        if status == "completed" and final_plan_id:
            recipe_ids = (health_evidence or {}).get("recipe_ids", [])
            cursor.execute(
                "INSERT INTO menu_versions (session_id, plan_id, recipe_ids) "
                "VALUES (%s, %s, %s)",
                (session_id, final_plan_id,
                 json.dumps(recipe_ids, ensure_ascii=False)),
            )
        # 4. 终态会话事件（依赖 sessions）——event_id 为主键，重跑/幂等用 ON DUPLICATE
        cursor.execute(
            "INSERT INTO conversation_events "
            "(event_id, session_id, request_id, event_type, event_summary) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE event_summary=VALUES(event_summary)",
            (f"ev_{request_id}", session_id, request_id, "terminal",
             f"terminal:{status}"),
        )
        conn.commit()
        return {"request_id": request_id, "status": status, "committed": True}
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise AuditCommitFailed(f"INV-010 audit commit failed: {e}") from e
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
