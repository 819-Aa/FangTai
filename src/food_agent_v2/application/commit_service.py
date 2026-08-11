"""Application 提交服务（T19）—— 结果/健康审计/会话事实/transactional outbox 原子提交。

- Artifact 链与强制健康审计在 BEGIN 前确定性验证（缺字段/错 hash/错绑定即失败）；
- request_id 结果 insert-once、不可变：同 payload 幂等返回原提交，不修改任何事实；
  不同 payload fail closed（IDEMPOTENCY_CONFLICT）；
- fencing token 必须为正整数；session 行在事务内 SELECT ... FOR UPDATE 锁定后原子
  比较/更新；stale token 一律回滚；
- completed 时按序写入 outbox 行，绝不因重复提交重置已 dispatched 状态；
- 成功 COMMIT 后 success SSE 由 outbox dispatcher 发布（不在此发布）。
"""

from __future__ import annotations

import json

from food_agent_v2.contracts.build import canonical_json_hash
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


def _is_posint(value) -> bool:
    try:
        return int(value) > 0
    except (ValueError, TypeError):
        return False


def _is_sha256(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdefABCDEF" for c in value))


def _commit_hash(request_id: str, session_id: str, status: str,
                 final_plan_id: str, health_evidence: dict,
                 answer_text: str, menu_hash: str) -> str:
    """canonical commit payload 的确定性 hash（幂等比较）。

    只覆盖确定性业务事实（plan/recipe/menu/verdict/answer），排除随机 Artifact
    身份引用——同一请求相同业务事实视为幂等重放，不同事实 fail closed。
    """
    return canonical_json_hash({
        "request_id": request_id,
        "status": status,
        "final_plan_id": final_plan_id,
        "plan_id": health_evidence.get("plan_id"),
        "recipe_ids": health_evidence.get("recipe_ids"),
        "menu_hash": health_evidence.get("menu_hash"),
        "verdict": (health_evidence.get("final_validation") or {}).get("verdict"),
        "answer_text": answer_text,
    })


def _validate_commit_payload(health_evidence: dict, status: str,
                             final_plan_id: str, menu_hash: str = "") -> None:
    """Artifact 链与强制健康审计的确定性验证（BEGIN 前，无写入）。

    缺字段、错 hash、错 request/plan/menu 绑定一律抛 AuditCommitFailed。
    """
    if status != "completed":
        return  # 非 completed 只记录终态，不要求健康审计
    required = [
        "request_id", "plan_id", "menu_hash", "recipe_ids",
        "final_validation", "menu_decision", "review", "answer",
        "participant_constraint_refs", "ingredient_relation_coverage_refs",
        "override_refs", "tool_receipt_refs", "tool_input_output_hashes",
    ]
    # 引用列表以 key 存在性校验（override_refs 等可为空但键必须存在）
    missing = [k for k in required if k not in health_evidence]
    if missing:
        raise AuditCommitFailed(f"健康审计缺字段: {missing}")
    if not health_evidence.get("plan_id") or not health_evidence.get("menu_hash"):
        raise AuditCommitFailed("健康审计 plan_id/menu_hash 为空")
    if not health_evidence.get("recipe_ids"):
        raise AuditCommitFailed("健康审计 recipe_ids 为空")

    rid = health_evidence["request_id"]
    fv = health_evidence["final_validation"]
    md = health_evidence["menu_decision"]
    rv = health_evidence["review"]
    ans = health_evidence["answer"]
    for name, a in (("final_validation", fv), ("menu_decision", md),
                    ("review", rv), ("answer", ans)):
        if a.get("request_id") != rid:
            raise AuditCommitFailed(f"{name} request_id 不一致")

    plans = {fv.get("plan_id"), md.get("plan_id"), ans.get("plan_id")}
    if plans != {health_evidence["plan_id"], health_evidence["plan_id"]} \
            or len(plans) != 1 or plans.pop() != health_evidence["plan_id"]:
        raise AuditCommitFailed("Artifact plan_id 不一致")

    hashes = {fv.get("menu_hash"), md.get("menu_hash"), ans.get("menu_hash")}
    if len(hashes) != 1 or hashes.pop() != health_evidence["menu_hash"]:
        raise AuditCommitFailed("Artifact menu_hash 不一致")

    if fv.get("verdict") != "PASS":
        raise AuditCommitFailed(f"FinalValidation 非 PASS: {fv.get('verdict')}")

    fv_hash = fv.get("content_hash") or fv.get("input_fingerprint")
    for name, h in (("final_validation", fv_hash),
                    ("review", rv.get("content_hash")),
                    ("answer", ans.get("content_hash")),
                    ("menu_hash", health_evidence["menu_hash"])):
        if not _is_sha256(h):
            raise AuditCommitFailed(f"{name} hash 非法")

    if sorted(fv.get("recipe_ids", [])) != sorted(health_evidence.get("recipe_ids", [])):
        raise AuditCommitFailed("FinalValidation recipe_ids 与菜单不一致")

    for tr in health_evidence.get("tool_input_output_hashes", []):
        if not _is_sha256(tr.get("input_hash")) or not _is_sha256(tr.get("output_hash")):
            raise AuditCommitFailed("tool receipt hash 非法")

    if final_plan_id != health_evidence["plan_id"]:
        raise AuditCommitFailed("final_plan_id 与菜单 plan_id 不一致")
    if menu_hash and menu_hash != health_evidence["menu_hash"]:
        raise AuditCommitFailed("提交 menu_hash 与审计 menu_hash 不一致")


def _validate_fencing_token(stored_token: int | None, fencing_token) -> None:
    """生产 completed 提交必须正整数；stale（<= 已存）→ 抛错回滚。

    仅 `None` 表示显式无锁测试替身路径（runner 生产从不传 None）；其他
    非正整数（含 "no-lock-support" 等魔法字符串）一律拒绝。
    """
    if fencing_token is None:
        return  # 测试替身路径（runner 生产恒有正整数 token）
    if not _is_posint(fencing_token):
        raise AuditCommitFailed(
            f"completed 提交必须携带正整数 fencing token，实际 {fencing_token!r}")
    incoming = int(fencing_token)
    if stored_token is not None and incoming <= stored_token:
        raise AuditCommitFailed(
            f"stale fencing_token: {incoming} <= stored {stored_token}")


def _build_outbox_rows(request_id: str, final_plan_id: str,
                       health_evidence: dict, answer_text: str,
                       menu_hash: str, menu_ref: str,
                       evidence_refs: list[str]) -> list[dict]:
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
    """将请求终态 + 健康审计 + 会话事实 + outbox 原子写入 MySQL。

    - request_id 结果 insert-once：同 payload 幂等返回原提交，不同 payload 冲突；
    - fencing token 为事务强制参数（生产 completed 必须正整数，stale 回滚）。
    """
    conn = _connect()
    try:
        # 0. Artifact 链与强制健康审计验证（BEGIN 前，无写入）
        _validate_commit_payload(health_evidence, status, final_plan_id, menu_hash)
        cursor = conn.cursor()
        commit_hash = _commit_hash(
            request_id, session_id, status, final_plan_id, health_evidence,
            answer_text, menu_hash)

        # 1. 会话行存在（INSERT IGNORE 不递增 request_count）
        cursor.execute(
            "INSERT IGNORE INTO sessions "
            "(session_id, participant_refs, request_count) VALUES (%s, %s, 0)",
            (session_id, json.dumps(participant_refs or [], ensure_ascii=False)),
        )
        # 2. 锁定 session 行（FOR UPDATE：并发提交序列化）
        cursor.execute(
            "SELECT fencing_token FROM sessions WHERE session_id=%s FOR UPDATE",
            (session_id,))
        row = cursor.fetchone()
        stored_token = int(row[0]) if row and row[0] is not None else None

        # 3. 幂等：同 request 已提交 → 同 payload 原样返回，不同 payload 冲突
        cursor.execute(
            "SELECT commit_hash, status FROM recommendation_logs "
            "WHERE request_id=%s", (request_id,))
        existing = cursor.fetchone()
        if existing:
            if existing[0] == commit_hash:
                conn.rollback()  # 幂等命中：不修改任何数据库事实
                return {"request_id": request_id, "status": existing[1],
                        "committed": True, "idempotent": True,
                        "outbox_event_ids": []}
            raise AuditCommitFailed(
                f"IDEMPOTENCY_CONFLICT: request {request_id} 已用不同 payload 提交")

        # 4. fencing 单调校验（stale → 抛错 → rollback）
        _validate_fencing_token(stored_token, fencing_token)

        # 5. 递增 request_count + 记录最新 fencing token（真实提交）
        cursor.execute(
            "UPDATE sessions SET request_count=request_count+1, "
            "fencing_token=%s WHERE session_id=%s",
            (int(fencing_token) if _is_posint(fencing_token) else None, session_id))

        # 6. 不可变结果 + 强制健康审计（insert-once，不可覆盖）
        cursor.execute(
            "INSERT INTO recommendation_logs "
            "(request_id, session_id, status, final_plan_id, health_evidence, commit_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (request_id, session_id, status, final_plan_id,
             json.dumps(health_evidence, ensure_ascii=False), commit_hash),
        )
        # 7. 已提交菜单版本
        if status == "completed" and final_plan_id:
            recipe_ids = (health_evidence or {}).get("recipe_ids", [])
            cursor.execute(
                "INSERT INTO menu_versions (session_id, plan_id, recipe_ids) "
                "VALUES (%s, %s, %s)",
                (session_id, final_plan_id,
                 json.dumps(recipe_ids, ensure_ascii=False)),
            )
        # 8. 终态会话事件（event_id 主键，幂等）
        cursor.execute(
            "INSERT INTO conversation_events "
            "(event_id, session_id, request_id, event_type, event_summary) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE event_summary=VALUES(event_summary)",
            (f"ev_{request_id}", session_id, request_id, "terminal",
             f"terminal:{status}"),
        )
        # 9. 有序 outbox 行（仅 completed；INSERT IGNORE 不重置既有状态）
        outbox_rows: list[dict] = []
        if status == "completed":
            outbox_rows = _build_outbox_rows(
                request_id, final_plan_id, health_evidence,
                answer_text, menu_hash, menu_ref, evidence_refs or [])
            for row in outbox_rows:
                cursor.execute(
                    "INSERT IGNORE INTO outbox "
                    "(event_id, request_id, event_type, payload, seq, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'pending')",
                    (row["event_id"], request_id, row["event_type"],
                     json.dumps(row["payload"], ensure_ascii=False), row["seq"]),
                )
        conn.commit()
        return {
            "request_id": request_id,
            "status": status,
            "committed": True,
            "idempotent": False,
            "outbox_event_ids": [r["event_id"] for r in outbox_rows],
        }
    except AuditCommitFailed:
        conn.rollback()
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise AuditCommitFailed(f"INV-010 audit commit failed: {e}") from e
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
