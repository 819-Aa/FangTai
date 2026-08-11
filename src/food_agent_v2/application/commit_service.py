"""Application 提交服务（T19）—— 结果/健康审计/会话事实/transactional outbox 原子提交。

同一事务内：
- 按 session 校验单调 fencing token（stale token 一律回滚）；
- 写入不可变结果（recommendation_logs）；
- 写入强制健康审计（health_evidence，同事务）；
- 写入已提交会话事实（sessions + conversation_events + menu_versions）；
- completed 时按序写入 answer_ready/result_committed outbox 行；
- 记录最新 fencing token；
成功 COMMIT 后，success SSE 由 outbox dispatcher 发布（不在此发布）。
"""

from __future__ import annotations

import json

from food_agent_v2.core.config import load_config


class AuditCommitFailed(Exception):
    """INV-010 原子提交失败（任一写入失败即回滚）。"""


def _connect():
    import pymysql

    cfg = load_config()
    return pymysql.connect(
        host=cfg.mysql.host, port=cfg.mysql.port,
        user=cfg.mysql.user, password=cfg.mysql.password,
        database=cfg.mysql.database, charset="utf8mb4",
        autocommit=False,
    )


def _is_int(value) -> bool:
    try:
        int(value)
        return True
    except (ValueError, TypeError):
        return False


def _validate_fencing_token(cursor, session_id: str, fencing_token: str | None) -> None:
    """按 session 校验单调 fencing token；stale（<= 已存）→ 抛错回滚。

    无锁/测试双（None 或 "no-lock-support"）场景不校验单调性。
    """
    if fencing_token in (None, "", "no-lock-support"):
        return
    if not _is_int(fencing_token):
        raise AuditCommitFailed(f"非法 fencing_token: {fencing_token!r}")
    incoming = int(fencing_token)
    cursor.execute(
        "SELECT fencing_token FROM sessions WHERE session_id=%s", (session_id,))
    row = cursor.fetchone()
    if row and row[0]:
        stored = int(row[0]) if _is_int(row[0]) else -1
        if incoming <= stored:
            raise AuditCommitFailed(
                f"stale fencing_token: {incoming} <= stored {stored}")


def _build_outbox_rows(request_id: str, status: str, final_plan_id: str,
                       health_evidence: dict, answer_text: str,
                       menu_hash: str, menu_ref: str,
                       evidence_refs: list[str]) -> list[dict]:
    """completed 时的有序 outbox 行（answer_ready → result_committed）。"""
    recipe_ids = (health_evidence or {}).get("recipe_ids", [])
    return [
        {
            "event_id": f"ev_answer_{request_id}",
            "event_type": "answer_ready",
            "seq": 1,
            "payload": {
                "text": answer_text,
                "menu_ref": menu_ref or menu_hash,
                "evidence_refs": evidence_refs or [],
            },
        },
        {
            "event_id": f"ev_result_{request_id}",
            "event_type": "result_committed",
            "seq": 2,
            "payload": {
                "menu_summary": {
                    "plan_id": final_plan_id,
                    "menu_hash": menu_hash,
                    "recipe_ids": recipe_ids,
                },
            },
        },
    ]


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
    answer_text: str = "",
    menu_hash: str = "",
    menu_ref: str = "",
    evidence_refs: list[str] | None = None,
) -> dict:
    """将请求终态 + 健康审计 + 会话事实 + outbox 原子写入 MySQL（INV-010 / T19）。

    fencing token 为事务强制参数（真实提交路径始终携带）；stale token 回滚。
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        # 0. 单调 fencing token 校验（stale → 抛错 → rollback）
        _validate_fencing_token(cursor, session_id, fencing_token)

        # 1. 会话（父表，先建）
        cursor.execute(
            "INSERT INTO sessions (session_id, participant_refs, request_count) "
            "VALUES (%s, %s, 1) "
            "ON DUPLICATE KEY UPDATE request_count=request_count+1",
            (session_id, json.dumps(participant_refs or [], ensure_ascii=False)),
        )
        # 2. 不可变结果 + 强制健康审计（同一事务）
        audit_payload = dict(health_evidence or {})
        cursor.execute(
            "INSERT INTO recommendation_logs "
            "(request_id, session_id, status, final_plan_id, health_evidence) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE status=VALUES(status), "
            "final_plan_id=VALUES(final_plan_id), health_evidence=VALUES(health_evidence)",
            (request_id, session_id, status, final_plan_id,
             json.dumps(audit_payload, ensure_ascii=False)),
        )
        # 3. 已提交菜单版本（依赖 sessions）
        if status == "completed" and final_plan_id:
            recipe_ids = (health_evidence or {}).get("recipe_ids", [])
            cursor.execute(
                "INSERT INTO menu_versions (session_id, plan_id, recipe_ids) "
                "VALUES (%s, %s, %s)",
                (session_id, final_plan_id,
                 json.dumps(recipe_ids, ensure_ascii=False)),
            )
        # 4. 终态会话事件（依赖 sessions）——event_id 主键，幂等用 ON DUPLICATE
        cursor.execute(
            "INSERT INTO conversation_events "
            "(event_id, session_id, request_id, event_type, event_summary) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE event_summary=VALUES(event_summary)",
            (f"ev_{request_id}", session_id, request_id, "terminal",
             f"terminal:{status}"),
        )
        # 5. 有序 outbox 行（仅 completed；与结果/审计/会话同事务）
        outbox_rows: list[dict] = []
        if status == "completed":
            outbox_rows = _build_outbox_rows(
                request_id, status, final_plan_id, health_evidence,
                answer_text, menu_hash, menu_ref, evidence_refs or [])
            for row in outbox_rows:
                cursor.execute(
                    "INSERT INTO outbox "
                    "(event_id, request_id, event_type, payload, seq, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'pending') "
                    "ON DUPLICATE KEY UPDATE payload=VALUES(payload), status='pending'",
                    (row["event_id"], request_id, row["event_type"],
                     json.dumps(row["payload"], ensure_ascii=False), row["seq"]),
                )
        # 6. 记录最新 fencing token（成功提交的单调上界）
        if fencing_token and fencing_token != "no-lock-support" and _is_int(fencing_token):
            cursor.execute(
                "UPDATE sessions SET fencing_token=%s WHERE session_id=%s",
                (str(int(fencing_token)), session_id),
            )
        conn.commit()
        return {
            "request_id": request_id,
            "status": status,
            "committed": True,
            "outbox_event_ids": [r["event_id"] for r in outbox_rows],
        }
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise AuditCommitFailed(f"INV-010 audit commit failed: {e}") from e
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
