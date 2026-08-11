"""Transactional outbox + dispatcher（T19）。

- success SSE（answer_ready / result_committed）只由事务提交后的 outbox dispatcher 发布；
- 同一 request 严格按 seq 发布：前序事件失败或仍 pending 时禁止发布任何后序事件；
- 稳定 outbox event_id 传入 D1 SSE（相同 event_id 重复发布只产生一份事实）；
- 原子领取（claim）防双 dispatcher 重复发布；发布后标记 dispatched；崩溃可补发；
- unknown event_type fail closed（不静默标记 dispatched）。
"""

from __future__ import annotations

import json

from food_agent_v2.core.config import load_config


class OutboxDispatcher:
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
            client_flag=pymysql.constants.CLIENT.FOUND_ROWS,
        )
        self._cursor = self._connection.cursor()
        return self._connection

    def _d1(self):
        if self._d1_api is None:
            from food_agent_v2.d1 import api as d1_api
            self._d1_api = d1_api
        return self._d1_api

    def _claim(self, event_id: str) -> bool:
        """原子领取 pending/dispatching 行；只有一个 dispatcher 成功。"""
        self._connect()
        self._cursor.execute(
            "UPDATE outbox SET status='dispatching' "
            "WHERE event_id=%s AND status IN ('pending','dispatching')",
            (event_id,))
        return self._cursor.rowcount == 1

    def _mark_dispatched(self, event_id: str) -> None:
        self._connect()
        self._cursor.execute(
            "UPDATE outbox SET status='dispatched', dispatched_at=NOW() "
            "WHERE event_id=%s", (event_id,))

    def _release_claim(self, event_id: str) -> None:
        self._connect()
        self._cursor.execute(
            "UPDATE outbox SET status='pending' WHERE event_id=%s", (event_id,))

    def fetch_pending(self, request_id: str | None = None, limit: int = 100) -> list[dict]:
        self._connect()
        if request_id:
            self._cursor.execute(
                "SELECT event_id, request_id, event_type, payload, seq "
                "FROM outbox WHERE status IN ('pending','dispatching') "
                "AND request_id=%s ORDER BY seq LIMIT %s", (request_id, limit))
        else:
            self._cursor.execute(
                "SELECT event_id, request_id, event_type, payload, seq "
                "FROM outbox WHERE status IN ('pending','dispatching') "
                "ORDER BY request_id, seq LIMIT %s", (limit,))
        rows = []
        for event_id, rid, event_type, payload, seq in self._cursor.fetchall():
            rows.append({
                "event_id": event_id, "request_id": rid,
                "event_type": event_type,
                "payload": json.loads(payload or "{}"), "seq": seq,
            })
        return rows

    def dispatch_request(self, request_id: str) -> int:
        """按 seq 严格发布某请求全部待投递事件；前序失败即停。"""
        return self._dispatch_ordered(self.fetch_pending(request_id))

    def dispatch_pending(self, request_id: str | None = None, limit: int = 100) -> int:
        """发布所有待投递事件（各 request 独立按 seq 严格有序）。"""
        rows = self.fetch_pending(request_id, limit)
        # 按 request_id 分组，组内按 seq 有序；组间独立
        by_request: dict[str, list[dict]] = {}
        for r in rows:
            by_request.setdefault(r["request_id"], []).append(r)
        total = 0
        for rid in sorted(by_request):
            total += self._dispatch_ordered(by_request[rid])
        return total

    def _dispatch_ordered(self, rows: list[dict]) -> int:
        """严格按 seq 发布；任一事件失败即停止该 request 后序事件。"""
        dispatched = 0
        for row in rows:
            if not self._claim(row["event_id"]):
                continue  # 已被其他 dispatcher 领取
            try:
                self._publish(row["request_id"], row["event_type"], row["payload"])
            except Exception:
                self._release_claim(row["event_id"])  # 恢复 pending 供重试
                return dispatched  # 前序失败 → 禁止后序
            self._mark_dispatched(row["event_id"])
            dispatched += 1
        return dispatched

    def _publish(self, request_id: str, event_type: str, payload: dict) -> None:
        """以稳定 outbox event_id 发布到 D1（相同 event_id 重复发布只产生一份事实）。"""
        d1 = self._d1()
        if event_type == "answer_ready":
            d1.publish_answer_event(
                request_id, payload.get("text", ""),
                payload.get("menu_ref", ""),
                payload.get("evidence_refs", []),
                event_id=f"ev_answer_{request_id}",
            )
        elif event_type == "result_committed":
            d1.publish_result_committed(
                request_id, payload.get("menu_summary", {}),
                event_id=f"ev_result_{request_id}",
            )
        else:
            raise RuntimeError(f"unknown outbox event_type: {event_type}")


def dispatch_request(request_id: str) -> int:
    """便捷入口：发布指定请求的 success SSE（事务提交后调用）。"""
    return OutboxDispatcher().dispatch_request(request_id)
