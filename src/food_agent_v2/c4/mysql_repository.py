"""C4 MySQL 会话记忆 Repository（T18）—— 已提交边界的事实来源。

MySQL 保存永久/已提交会话事实（sessions / conversation_events / menu_versions），
Redis 只保存可恢复运行快照。本模块是 C4 读取 MySQL 已提交边界的唯一入口；
内存 Fake 仅供单元测试（真实实现仅在 MySQL 可用时使用）。
"""

from __future__ import annotations

import json
import time
from typing import Protocol

from food_agent_v2.core.config import load_config


class SessionMemorySource(Protocol):
    """已提交会话记忆来源（MySQL 生产实现或测试 Fake）。"""

    def load_session(self, session_id: str) -> dict | None: ...
    def load_committed_events(self, session_id: str) -> list[dict]: ...
    def load_menu_versions(self, session_id: str) -> list[dict]: ...
    def load_query_plan(self, session_id: str, plan_id: str) -> dict | None: ...
    def is_clarification_committed(self, session_id: str, question_id: str) -> bool: ...
    def load_active_clarification(self, session_id: str) -> dict | None: ...
    def load_produced_active_clarification(self, request_id: str) -> dict | None: ...
    def load_clarification_state(self, session_id: str) -> dict | None: ...
    def register_active_fencing_token(self, session_id: str, token: int) -> bool: ...
    def load_request_acceptance(self, idempotency_key_hash: str) -> dict | None: ...
    def load_request_acceptance_by_request_id(self, request_id: str) -> dict | None: ...
    def claim_request_acceptance(
        self, idempotency_key_hash: str, payload_hash: str, request_id: str, session_id: str
    ) -> tuple[str, dict | None]: ...
    def claim_request_execution(self, request_id: str, owner: str, lease_seconds: int) -> bool: ...
    def finish_request_execution(self, request_id: str, owner: str, terminal: bool) -> None: ...
    def claim_request_execution_v2(self, request_id: str, owner: str, lease_seconds: int) -> dict | None: ...
    def renew_request_execution_v2(self, request_id: str, owner: str, generation: int, lease_seconds: int) -> bool: ...
    def finish_request_execution_v2(self, request_id: str, owner: str, generation: int, terminal: bool) -> bool: ...
    def load_recommendation_log(self, request_id: str) -> dict | None: ...
    def load_dispatched_outbox_events(self, request_id: str) -> list[dict]: ...
    def save_session(
        self,
        session_id: str,
        participant_refs: list[str],
        workflow_mode: str | None = None,
        clarification_protocol_version: str | None = None,
    ) -> None: ...


class MySQLSessionMemorySource:
    """从 MySQL sessions/conversation_events/menu_versions 读取已提交会话记忆。"""

    def __init__(self) -> None:
        self._connection = None
        self._cursor = None

    def _connect(self):
        if self._connection is not None:
            return self._connection
        import pymysql

        cfg = load_config().mysql
        self._connection = pymysql.connect(
            host=cfg.host, port=cfg.port,
            user=cfg.user, password=cfg.password,
            database=cfg.database, charset="utf8mb4",
            autocommit=True,
        )
        self._cursor = self._connection.cursor()
        return self._connection

    @property
    def cursor(self):
        self._connect()
        return self._cursor

    def load_session(self, session_id: str) -> dict | None:
        self.cursor.execute(
            "SELECT session_id, participant_refs, current_menu_plan_id, request_count, "
            "last_request_at "
            "FROM sessions WHERE session_id=%s", (session_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        return {
            "session_id": row[0],
            "participant_refs": json.loads(row[1] or "[]"),
            "current_menu_plan_id": row[2],
            "request_count": int(row[3] or 0),
            "last_request_at": str(row[4]) if row[4] else None,
        }

    def save_session(
        self,
        session_id: str,
        participant_refs: list[str],
        workflow_mode: str | None = None,
        clarification_protocol_version: str | None = None,
    ) -> None:
        """持久化会话边界（INSERT IGNORE；供 C4 公开 create 接口）。"""
        self.cursor.execute(
            "INSERT IGNORE INTO sessions "
            "(session_id, participant_refs, request_count, workflow_mode, clarification_protocol_version) "
            "VALUES (%s, %s, 0, %s, %s)",
            (
                session_id,
                json.dumps(participant_refs, ensure_ascii=False),
                workflow_mode,
                clarification_protocol_version,
            ),
        )

    def load_committed_events(self, session_id: str) -> list[dict]:
        self.cursor.execute(
            "SELECT event_id, request_id, event_type, event_summary, event_detail_ref, "
            "participant_refs, token_count_estimate "
            "FROM conversation_events WHERE session_id=%s ORDER BY created_at",
            (session_id,))
        events = []
        for row in self.cursor.fetchall():
            events.append({
                "event_id": row[0],
                "request_id": row[1],
                "event_type": row[2],
                "event_summary": row[3],
                "event_detail_ref": row[4],
                "participant_refs": json.loads(row[5] or "[]"),
                "token_count_estimate": int(row[6] or 0),
            })
        return events

    def load_menu_versions(self, session_id: str) -> list[dict]:
        self.cursor.execute(
            "SELECT plan_id, menu_hash, recipe_ids, committed_at "
            "FROM menu_versions WHERE session_id=%s ORDER BY committed_at",
            (session_id,))
        versions = []
        for row in self.cursor.fetchall():
            versions.append({
                "plan_id": row[0],
                "menu_hash": row[1],
                "recipe_ids": json.loads(row[2] or "[]"),
                "committed_at": str(row[3]) if row[3] else None,
            })
        return versions

    def load_query_plan(self, session_id: str, plan_id: str) -> dict | None:
        """读取当前菜单对应的最近 completed QueryPlan；旧审计无快照时返回 None。"""
        self.cursor.execute(
            "SELECT health_evidence FROM recommendation_logs "
            "WHERE session_id=%s AND final_plan_id=%s AND status='completed' "
            "ORDER BY created_at DESC, log_id DESC LIMIT 1",
            (session_id, plan_id),
        )
        row = self.cursor.fetchone()
        if not row or not row[0]:
            return None
        evidence = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        query_plan = evidence.get("query_plan") if isinstance(evidence, dict) else None
        return dict(query_plan) if isinstance(query_plan, dict) else None

    def is_clarification_committed(self, session_id: str, question_id: str) -> bool:
        """以 MySQL 已提交事实判定澄清选项是否已用于菜单或再次追问。"""
        self.cursor.execute(
            "SELECT 1 FROM recommendation_logs "
            "WHERE session_id=%s AND status IN ('completed', 'needs_clarification') "
            "AND JSON_UNQUOTE(JSON_EXTRACT(health_evidence, "
            "'$.accepted_clarification_question_id'))=%s LIMIT 1",
            (session_id, question_id),
        )
        return self.cursor.fetchone() is not None

    def load_active_clarification(self, session_id: str) -> dict | None:
        """从 MySQL 加载当前会话活跃的澄清问题（未过期且 pending）。"""
        self.cursor.execute(
            """
            SELECT q.question_id, q.session_id, q.status, q.public_payload, q.private_snapshot,
                   UNIX_TIMESTAMP(q.expires_at), q.accepted_request_id, q.accepted_option_id
            FROM sessions s
            JOIN clarification_questions q ON s.active_clarification_question_id = q.question_id
            WHERE s.session_id = %s
              AND q.status = 'pending'
              AND (q.expires_at IS NULL OR q.expires_at > CURRENT_TIMESTAMP)
            """,
            (session_id,),
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        pub = json.loads(row[3]) if isinstance(row[3], str) else row[3]
        priv = json.loads(row[4]) if isinstance(row[4], str) else row[4]
        return {
            "question_id": row[0],
            "session_id": row[1],
            "status": row[2],
            "public_payload": pub,
            "private_snapshot": priv,
            "expires_at": float(row[5]) if row[5] is not None else None,
            "accepted_request_id": row[6],
            "accepted_option_id": row[7],
        }

    def load_produced_active_clarification(self, request_id: str) -> dict | None:
        """Question produced by a request only while it remains the session's active question."""
        self.cursor.execute(
            """
            SELECT q.question_id, q.session_id, q.status, q.public_payload, q.private_snapshot,
                   UNIX_TIMESTAMP(q.expires_at), q.accepted_request_id, q.accepted_option_id
            FROM clarification_questions q
            JOIN sessions s ON s.session_id = q.session_id
                           AND s.active_clarification_question_id = q.question_id
            WHERE q.producer_request_id = %s
              AND q.status = 'pending'
              AND (q.expires_at IS NULL OR q.expires_at > CURRENT_TIMESTAMP)
            """,
            (request_id,),
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        return {
            "question_id": row[0], "session_id": row[1], "status": row[2],
            "public_payload": json.loads(row[3]) if isinstance(row[3], str) else row[3],
            "private_snapshot": json.loads(row[4]) if isinstance(row[4], str) else row[4],
            "expires_at": float(row[5]) if row[5] is not None else None,
            "accepted_request_id": row[6], "accepted_option_id": row[7],
        }

    def load_clarification_state(self, session_id: str) -> dict | None:
        """读取会话的澄清协议状态、活跃问题指针、版本与 fencing watermark。"""
        self.cursor.execute(
            """
            SELECT session_id, active_clarification_question_id, clarification_revision,
                   active_fencing_token, fencing_token, workflow_mode, clarification_protocol_version
            FROM sessions
            WHERE session_id = %s
            """,
            (session_id,),
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        return {
            "session_id": row[0],
            "active_question_id": row[1],
            "clarification_revision": int(row[2] or 0),
            "active_fencing_token": int(row[3]) if row[3] is not None else None,
            "fencing_token": int(row[4] or 0),
            "workflow_mode": row[5],
            "protocol_version": row[6],
        }

    def register_active_fencing_token(self, session_id: str, token: int) -> bool:
        """在会话表原子比较并登记 active_fencing_token。

        若 token <= 已登记 active_fencing_token 或 token <= 已提交 fencing_token 则登记失败返回 False。
        若会话不存在，则使用 INSERT INTO sessions 登记，若主键冲突则按上述规则更新。
        """
        conn = self._connect()
        try:
            conn.autocommit(False)
            self.cursor.execute(
                "SELECT active_fencing_token, fencing_token FROM sessions WHERE session_id = %s FOR UPDATE",
                (session_id,)
            )
            row = self.cursor.fetchone()
            if row is not None:
                active_token, committed_token = row[0], row[1]
                if active_token is not None and token <= int(active_token):
                    conn.rollback()
                    return False
                if committed_token is not None and token <= int(committed_token):
                    conn.rollback()
                    return False
                self.cursor.execute(
                    "UPDATE sessions SET active_fencing_token = %s WHERE session_id = %s",
                    (token, session_id),
                )
            else:
                self.cursor.execute(
                    "INSERT INTO sessions (session_id, participant_refs, request_count, active_fencing_token) "
                    "VALUES (%s, '[]', 0, %s)",
                    (session_id, token),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit(True)

    def load_request_acceptance(self, idempotency_key_hash: str) -> dict | None:
        self.cursor.execute(
            """
            SELECT idempotency_key_hash, payload_hash, request_id, session_id, status,
                   UNIX_TIMESTAMP(created_at), execution_owner, execution_generation,
                   UNIX_TIMESTAMP(execution_lease_until)
            FROM request_acceptances
            WHERE idempotency_key_hash = %s
            """,
            (idempotency_key_hash,),
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        return {
            "idempotency_key_hash": row[0],
            "payload_hash": row[1],
            "request_id": row[2],
            "session_id": row[3],
            "status": row[4],
            "created_at": float(row[5]) if row[5] is not None else None,
            "execution_owner": row[6],
            "execution_generation": int(row[7]),
            "execution_lease_until": float(row[8]) if row[8] is not None else None,
        }

    def load_request_acceptance_by_request_id(self, request_id: str) -> dict | None:
        self.cursor.execute(
            """
            SELECT idempotency_key_hash, payload_hash, request_id, session_id, status,
                   UNIX_TIMESTAMP(created_at), execution_owner, execution_generation,
                   UNIX_TIMESTAMP(execution_lease_until)
            FROM request_acceptances
            WHERE request_id = %s
            """,
            (request_id,),
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        return {
            "idempotency_key_hash": row[0],
            "payload_hash": row[1],
            "request_id": row[2],
            "session_id": row[3],
            "status": row[4],
            "created_at": float(row[5]) if row[5] is not None else None,
            "execution_owner": row[6],
            "execution_generation": int(row[7]),
            "execution_lease_until": float(row[8]) if row[8] is not None else None,
        }

    def claim_request_acceptance(
        self, idempotency_key_hash: str, payload_hash: str, request_id: str, session_id: str,
        _attempt: int = 0,
    ) -> tuple[str, dict | None]:
        conn = self._connect()
        try:
            conn.autocommit(False)
            self.cursor.execute(
                """
                SELECT idempotency_key_hash, payload_hash, request_id, session_id, status,
                       UNIX_TIMESTAMP(created_at), execution_owner, execution_generation,
                       UNIX_TIMESTAMP(execution_lease_until)
                FROM request_acceptances
                WHERE idempotency_key_hash = %s
                FOR UPDATE
                """,
                (idempotency_key_hash,),
            )
            row = self.cursor.fetchone()
            if row:
                conn.rollback()
                return ("existing", {
                    "idempotency_key_hash": row[0],
                    "payload_hash": row[1],
                    "request_id": row[2],
                    "session_id": row[3],
                    "status": row[4],
                    "created_at": float(row[5]) if row[5] is not None else None,
                    "execution_owner": row[6],
                    "execution_generation": int(row[7]),
                    "execution_lease_until": float(row[8]) if row[8] is not None else None,
                })
            self.cursor.execute(
                """
                INSERT INTO request_acceptances (idempotency_key_hash, payload_hash, request_id, session_id, status)
                VALUES (%s, %s, %s, %s, 'accepted')
                """,
                (idempotency_key_hash, payload_hash, request_id, session_id),
            )
            conn.commit()
            return ("winner", {
                "idempotency_key_hash": idempotency_key_hash,
                "payload_hash": payload_hash,
                "request_id": request_id,
                "session_id": session_id,
                "status": "accepted",
                "created_at": time.time(),
                "execution_owner": None,
                "execution_generation": 0,
                "execution_lease_until": None,
            })
        except Exception as exc:
            conn.rollback()
            import pymysql
            if isinstance(exc, (pymysql.err.IntegrityError, pymysql.err.OperationalError)) and \
                    exc.args and exc.args[0] in (1062, 1205, 1213):
                for _ in range(10):
                    conn.rollback()
                    winner = self.load_request_acceptance(idempotency_key_hash)
                    if winner:
                        return ("existing", winner)
                    time.sleep(0.05)
                if exc.args[0] in (1205, 1213) and _attempt < 2:
                    return self.claim_request_acceptance(
                        idempotency_key_hash, payload_hash, request_id, session_id, _attempt + 1,
                    )
            raise
        finally:
            conn.autocommit(True)

    def claim_request_execution(self, request_id: str, owner: str, lease_seconds: int) -> bool:
        """CAS one execution lease; expired workers are fenced by the workflow lock/token."""
        self.cursor.execute(
            "UPDATE request_acceptances SET status='running', execution_owner=%s, "
            "execution_lease_until=DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND) "
            "WHERE request_id=%s AND (status IN ('accepted', 'recovery_required') "
            "OR (status='running' AND execution_lease_until<CURRENT_TIMESTAMP))",
            (owner, lease_seconds, request_id),
        )
        return self.cursor.rowcount == 1

    def claim_request_execution_v2(self, request_id: str, owner: str, lease_seconds: int) -> dict | None:
        """Claim one generation using the DB clock; no terminal result may be reclaimed."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.cursor.execute(
            "UPDATE request_acceptances AS ra SET status='running', execution_owner=%s, "
            "execution_generation=execution_generation+1, "
            "execution_lease_until=DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND) "
            "WHERE ra.request_id=%s AND (ra.status IN ('accepted', 'recovery_required') "
            "OR (ra.status='running' AND ra.execution_lease_until<CURRENT_TIMESTAMP)) "
            "AND NOT EXISTS (SELECT 1 FROM recommendation_logs AS rl WHERE rl.request_id=ra.request_id)",
            (owner, lease_seconds, request_id),
        )
        if self.cursor.rowcount != 1:
            return None
        self.cursor.execute(
            "SELECT execution_generation, UNIX_TIMESTAMP(execution_lease_until) "
            "FROM request_acceptances WHERE request_id=%s AND execution_owner=%s",
            (request_id, owner),
        )
        generation, lease_until = self.cursor.fetchone()
        return {"owner": owner, "generation": int(generation), "lease_until": float(lease_until)}

    def renew_request_execution_v2(
        self, request_id: str, owner: str, generation: int, lease_seconds: int,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.cursor.execute(
            "UPDATE request_acceptances SET "
            "execution_lease_until=DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND) "
            "WHERE request_id=%s AND execution_owner=%s AND execution_generation=%s "
            "AND status='running' AND execution_lease_until>CURRENT_TIMESTAMP",
            (lease_seconds, request_id, owner, generation),
        )
        if self.cursor.rowcount == 1:
            return True
        # A renewal in the same DB second can leave TIMESTAMP unchanged, so
        # MySQL reports zero changed rows even though this owner still holds it.
        self.cursor.execute(
            "SELECT 1 FROM request_acceptances WHERE request_id=%s AND execution_owner=%s "
            "AND execution_generation=%s AND status='running' "
            "AND execution_lease_until>CURRENT_TIMESTAMP",
            (request_id, owner, generation),
        )
        return self.cursor.fetchone() is not None

    def finish_request_execution_v2(
        self, request_id: str, owner: str, generation: int, terminal: bool,
    ) -> bool:
        """Release a live generation; terminal requires an already committed result."""
        self.cursor.execute(
            "UPDATE request_acceptances AS ra SET status=%s, execution_lease_until=NULL "
            "WHERE ra.request_id=%s AND ra.execution_owner=%s AND ra.execution_generation=%s "
            "AND ra.status='running' AND ra.execution_lease_until>CURRENT_TIMESTAMP "
            "AND (%s=0 OR EXISTS (SELECT 1 FROM recommendation_logs AS rl WHERE rl.request_id=ra.request_id))",
            ("terminal" if terminal else "recovery_required", request_id, owner,
             generation, 1 if terminal else 0),
        )
        return self.cursor.rowcount == 1

    def finish_request_execution(self, request_id: str, owner: str, terminal: bool) -> None:
        self.cursor.execute(
            "UPDATE request_acceptances SET status=%s, execution_lease_until=NULL "
            "WHERE request_id=%s AND execution_owner=%s AND status='running'",
            ("terminal" if terminal else "recovery_required", request_id, owner),
        )

    def load_recommendation_log(self, request_id: str) -> dict | None:
        self.cursor.execute(
            """
            SELECT request_id, session_id, status, final_plan_id, health_evidence, commit_hash,
                   UNIX_TIMESTAMP(created_at)
            FROM recommendation_logs
            WHERE request_id = %s
            """,
            (request_id,),
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        evidence = json.loads(row[4]) if isinstance(row[4], str) else row[4]
        return {
            "request_id": row[0],
            "session_id": row[1],
            "status": row[2],
            "final_plan_id": row[3],
            "health_evidence": evidence,
            "commit_hash": row[5],
            "created_at": float(row[6]) if row[6] is not None else None,
        }

    def load_dispatched_outbox_events(self, request_id: str) -> list[dict]:
        self.cursor.execute(
            """
            SELECT event_id, event_type, payload, seq, status, UNIX_TIMESTAMP(created_at)
            FROM outbox
            WHERE request_id = %s AND status = 'dispatched'
            ORDER BY seq ASC
            """,
            (request_id,),
        )
        rows = self.cursor.fetchall()
        events = []
        for r in rows:
            p = json.loads(r[2]) if isinstance(r[2], str) else r[2]
            events.append({
                "event_id": r[0],
                "event_type": r[1],
                "payload": p,
                "seq": int(r[3]),
                "status": r[4],
                "created_at": float(r[5]) if r[5] is not None else None,
            })
        return events


class InMemorySessionMemorySource:
    """单元测试 Fake：内存中的已提交会话记忆。"""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.menus: dict[str, list[dict]] = {}
        self.query_plans: dict[tuple[str, str], dict] = {}
        self.committed_clarification_ids: set[tuple[str, str]] = set()
        self.clarification_questions: dict[str, dict] = {}
        self.request_acceptances: dict[str, dict] = {}
        self.recommendation_logs: dict[str, dict] = {}
        self.outbox_events: dict[str, list[dict]] = {}

    def load_request_acceptance(self, idempotency_key_hash: str) -> dict | None:
        rec = self.request_acceptances.get(idempotency_key_hash)
        return dict(rec) if rec is not None else None

    def load_request_acceptance_by_request_id(self, request_id: str) -> dict | None:
        for rec in self.request_acceptances.values():
            if rec.get("request_id") == request_id:
                return dict(rec)
        return None

    def claim_request_acceptance(
        self, idempotency_key_hash: str, payload_hash: str, request_id: str, session_id: str
    ) -> tuple[str, dict | None]:
        if idempotency_key_hash in self.request_acceptances:
            return ("existing", dict(self.request_acceptances[idempotency_key_hash]))
        rec = {
            "idempotency_key_hash": idempotency_key_hash,
            "payload_hash": payload_hash,
            "request_id": request_id,
            "session_id": session_id,
            "status": "accepted",
            "created_at": time.time(),
            "execution_owner": None,
            "execution_generation": 0,
            "execution_lease_until": None,
        }
        self.request_acceptances[idempotency_key_hash] = rec
        return ("winner", dict(rec))

    def claim_request_execution(self, request_id: str, owner: str, lease_seconds: int) -> bool:
        for rec in self.request_acceptances.values():
            if rec.get("request_id") != request_id:
                continue
            if rec.get("status") == "running" and rec.get("execution_lease_until", 0) > time.time():
                return False
            if rec.get("status") == "terminal":
                return False
            rec.update(status="running", execution_owner=owner,
                       execution_lease_until=time.time() + lease_seconds)
            return True
        return False

    def claim_request_execution_v2(self, request_id: str, owner: str, lease_seconds: int) -> dict | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if request_id in self.recommendation_logs:
            return None
        for rec in self.request_acceptances.values():
            if rec.get("request_id") != request_id:
                continue
            if rec["status"] == "running" and rec.get("execution_lease_until", 0) > time.time():
                return None
            if rec["status"] not in ("accepted", "recovery_required", "running"):
                return None
            generation = int(rec.get("execution_generation", 0)) + 1
            lease_until = time.time() + lease_seconds
            rec.update(status="running", execution_owner=owner,
                       execution_generation=generation, execution_lease_until=lease_until)
            return {"owner": owner, "generation": generation, "lease_until": lease_until}
        return None

    def renew_request_execution_v2(
        self, request_id: str, owner: str, generation: int, lease_seconds: int,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        for rec in self.request_acceptances.values():
            if (rec.get("request_id") == request_id and rec.get("execution_owner") == owner
                    and rec.get("execution_generation") == generation and rec.get("status") == "running"
                    and rec.get("execution_lease_until", 0) > time.time()):
                rec["execution_lease_until"] = time.time() + lease_seconds
                return True
        return False

    def finish_request_execution_v2(
        self, request_id: str, owner: str, generation: int, terminal: bool,
    ) -> bool:
        if terminal and request_id not in self.recommendation_logs:
            return False
        for rec in self.request_acceptances.values():
            if (rec.get("request_id") == request_id and rec.get("execution_owner") == owner
                    and rec.get("execution_generation") == generation and rec.get("status") == "running"
                    and rec.get("execution_lease_until", 0) > time.time()):
                rec.update(status="terminal" if terminal else "recovery_required",
                           execution_lease_until=None)
                return True
        return False

    def finish_request_execution(self, request_id: str, owner: str, terminal: bool) -> None:
        for rec in self.request_acceptances.values():
            if rec.get("request_id") == request_id and rec.get("execution_owner") == owner:
                rec.update(status="terminal" if terminal else "recovery_required",
                           execution_lease_until=None)

    def load_recommendation_log(self, request_id: str) -> dict | None:
        log = self.recommendation_logs.get(request_id)
        return dict(log) if log is not None else None

    def load_dispatched_outbox_events(self, request_id: str) -> list[dict]:
        events = self.outbox_events.get(request_id, [])
        return [dict(e) for e in events if e.get("status") == "dispatched"]

    def is_clarification_committed(self, session_id: str, question_id: str) -> bool:
        return (session_id, question_id) in self.committed_clarification_ids

    def load_active_clarification(self, session_id: str) -> dict | None:
        sess = self.sessions.get(session_id)
        if not sess:
            return None
        active_qid = sess.get("active_clarification_question_id")
        if not active_qid:
            return None
        q = self.clarification_questions.get(active_qid)
        if not q or q.get("status") != "pending":
            return None
        if q.get("expires_at") and time.time() > float(q["expires_at"]):
            return None
        return dict(q)

    def load_produced_active_clarification(self, request_id: str) -> dict | None:
        for session_id in self.sessions:
            active = self.load_active_clarification(session_id)
            if active and active.get("producer_request_id") == request_id:
                return active
        return None

    def load_clarification_state(self, session_id: str) -> dict | None:
        sess = self.sessions.get(session_id)
        if not sess:
            return None
        return {
            "session_id": session_id,
            "active_question_id": sess.get("active_clarification_question_id"),
            "clarification_revision": int(sess.get("clarification_revision", 0)),
            "active_fencing_token": sess.get("active_fencing_token"),
            "fencing_token": int(sess.get("fencing_token", 0)),
            "workflow_mode": sess.get("workflow_mode"),
            "protocol_version": sess.get("clarification_protocol_version"),
        }

    def register_active_fencing_token(self, session_id: str, token: int) -> bool:
        sess = self.sessions.setdefault(session_id, {
            "session_id": session_id,
            "participant_refs": [],
            "request_count": 0,
            "fencing_token": 0,
            "active_fencing_token": None,
        })
        active = sess.get("active_fencing_token")
        committed = sess.get("fencing_token", 0)
        if active is not None and token <= int(active):
            return False
        if committed is not None and token <= int(committed):
            return False
        sess["active_fencing_token"] = token
        return True

    def load_session(self, session_id: str) -> dict | None:
        meta = self.sessions.get(session_id)
        if meta is None:
            return None
        return dict(meta) | {"last_request_at": meta.get("last_request_at")}

    def load_committed_events(self, session_id: str) -> list[dict]:
        return list(self.events.get(session_id, []))

    def load_menu_versions(self, session_id: str) -> list[dict]:
        return list(self.menus.get(session_id, []))

    def load_query_plan(self, session_id: str, plan_id: str) -> dict | None:
        plan = self.query_plans.get((session_id, plan_id))
        return dict(plan) if plan is not None else None

    def save_session(
        self,
        session_id: str,
        participant_refs: list[str],
        workflow_mode: str | None = None,
        clarification_protocol_version: str | None = None,
    ) -> None:
        self.sessions[session_id] = {
            "session_id": session_id,
            "participant_refs": list(participant_refs),
            "current_menu_plan_id": None,
            "request_count": 0,
            "last_request_at": None,
            "active_clarification_question_id": None,
            "clarification_revision": 0,
            "active_fencing_token": None,
            "fencing_token": 0,
            "workflow_mode": workflow_mode,
            "clarification_protocol_version": clarification_protocol_version,
        }


def default_mysql_session_memory_source() -> MySQLSessionMemorySource:
    return MySQLSessionMemorySource()
