"""Transactional outbox + dispatcher（T19）。

success SSE（answer_ready / result_committed）只由事务提交后的 outbox dispatcher 发布；
稳定 event_id 幂等；重复投递不重复事实；单个事件失败不影响其他。
"""

from __future__ import annotations

import json

from food_agent_v2.core.config import load_config


class OutboxDispatcher:
    """读取 pending outbox 行，发布到 D1 SSE，标记 dispatched。"""

    def __init__(self, d1_api=None):
        self._d1_api = d1_api
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

    def _d1(self):
        if self._d1_api is None:
            from food_agent_v2.d1 import api as d1_api
            self._d1_api = d1_api
        return self._d1_api

    def fetch_pending(self, request_id: str | None = None, limit: int = 100) -> list[dict]:
        self._connect()
        if request_id:
            self._cursor.execute(
                "SELECT event_id, request_id, event_type, payload, seq "
                "FROM outbox WHERE status='pending' AND request_id=%s "
                "ORDER BY seq LIMIT %s", (request_id, limit))
        else:
            self._cursor.execute(
                "SELECT event_id, request_id, event_type, payload, seq "
                "FROM outbox WHERE status='pending' ORDER BY request_id, seq LIMIT %s",
                (limit,))
        rows = []
        for event_id, rid, event_type, payload, seq in self._cursor.fetchall():
            rows.append({
                "event_id": event_id, "request_id": rid,
                "event_type": event_type,
                "payload": json.loads(payload or "{}"), "seq": seq,
            })
        return rows

    def dispatch_request(self, request_id: str) -> int:
        """发布某请求全部 pending outbox 事件；返回已发布数。"""
        return self.dispatch_pending(request_id=request_id)

    def dispatch_pending(self, request_id: str | None = None, limit: int = 100) -> int:
        """发布 pending 事件并标记 dispatched（幂等，重复调用不重复事实）。"""
        pending = self.fetch_pending(request_id, limit)
        dispatched = 0
        for row in pending:
            try:
                self._publish(row["request_id"], row["event_type"], row["payload"])
            except Exception:
                continue  # 单个事件失败不影响其他；保持 pending 后续可补发
            self._mark_dispatched(row["event_id"])
            dispatched += 1
        return dispatched

    def _publish(self, request_id: str, event_type: str, payload: dict) -> None:
        d1 = self._d1()
        if event_type == "answer_ready":
            d1.publish_answer_event(
                request_id,
                payload.get("text", ""),
                payload.get("menu_ref", ""),
                payload.get("evidence_refs", []),
            )
        elif event_type == "result_committed":
            d1.publish_result_committed(request_id, payload.get("menu_summary", {}))

    def _mark_dispatched(self, event_id: str) -> None:
        self._connect()
        self._cursor.execute(
            "UPDATE outbox SET status='dispatched', dispatched_at=NOW() "
            "WHERE event_id=%s", (event_id,))


def dispatch_request(request_id: str) -> int:
    """便捷入口：发布指定请求的 success SSE（事务提交后调用）。"""
    return OutboxDispatcher().dispatch_request(request_id)
