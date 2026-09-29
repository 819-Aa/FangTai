"""Transactional outbox + dispatcher（T19）。

- success SSE（answer_ready / result_committed）只由事务提交后的 outbox dispatcher 发布；
- 同一 request 严格按 seq 发布：前序事件失败、仍 pending 或被其他 dispatcher in-flight
  持有时，一律禁止领取/发布任何后序事件（claim 内 NOT EXISTS 未 dispatched 前序）；
- 稳定 outbox event_id 传入 D1 SSE（相同 event_id 重复发布只产生一份事实）；
- claim 只从 pending 原子转换（claim_token + claimed_at 租约）；只有超出租约的
  dispatching 才允许恢复；mark/release 校验 claim_token 所有权 → 双 dispatcher 互斥；
- unknown event_type fail closed（不静默标记 dispatched）。
"""

from __future__ import annotations

import json
import uuid

from food_agent_v2.core.config import load_config


class OutboxDispatcher:
    #: dispatching 租约秒数：只有 claimed_at 早于当前-租约的行才可被其他 dispatcher 恢复。
    lease_seconds = 30

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

    def _claim(self, event_id: str, claim_token: str,
               request_id: str, seq: int) -> bool:
        """原子领取当前最小未完成 seq：只从 pending 转换，或恢复超出租约的 dispatching，
        且同 request 不存在未 dispatched 的前序行（NOT EXISTS）。

        两个 dispatcher 并发领取同一行：只有最先的 UPDATE 匹配（rowcount=1），
        第二个因状态已变且非 stale 而不匹配（rowcount=0）→ 互斥；若前序
        （seq 更小）仍 pending/dispatching，则本行也不可领取 → 严格保序。
        """
        self._connect()
        self._cursor.execute(
            "UPDATE outbox o "
            "LEFT JOIN outbox pred "
            "  ON pred.request_id = o.request_id "
            " AND pred.seq < o.seq "
            " AND pred.status <> 'dispatched' "
            "SET o.status='dispatching', o.claim_token=%s, o.claimed_at=NOW() "
            "WHERE o.event_id=%s "
            "  AND pred.event_id IS NULL "
            "  AND (o.status='pending' OR "
            "    (o.status='dispatching' AND "
            "     (o.claimed_at IS NULL OR "
            "      o.claimed_at < DATE_SUB(NOW(), INTERVAL %s SECOND))))",
            (claim_token, event_id, self.lease_seconds))
        return self._cursor.rowcount == 1

    def _mark_dispatched(self, event_id: str, claim_token: str) -> bool:
        """标记 dispatched：必须校验 claim_token 所有权。"""
        self._connect()
        self._cursor.execute(
            "UPDATE outbox SET status='dispatched', dispatched_at=NOW() "
            "WHERE event_id=%s AND claim_token=%s", (event_id, claim_token))
        return self._cursor.rowcount == 1

    def _release_claim(self, event_id: str, claim_token: str) -> None:
        """释放领取（失败重试）：必须校验 claim_token 所有权。"""
        self._connect()
        self._cursor.execute(
            "UPDATE outbox SET status='pending', claim_token=NULL, claimed_at=NULL "
            "WHERE event_id=%s AND claim_token=%s", (event_id, claim_token))

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
        rows = self.fetch_pending(request_id, limit)
        by_request: dict[str, list[dict]] = {}
        for r in rows:
            by_request.setdefault(r["request_id"], []).append(r)
        total = 0
        for rid in sorted(by_request):
            total += self._dispatch_ordered(by_request[rid])
        return total

    def _dispatch_ordered(self, rows: list[dict]) -> int:
        """严格按 seq 发布；任一事件失败、前序未完成或领取失败即停止该 request。

        领取失败（行被其他 dispatcher in-flight 持有，或前序未 dispatched）
        必须立即停止：继续 seq2 会让 result_committed 早于 answer_ready 发布。
        """
        dispatched = 0
        for row in rows:
            claim_token = uuid.uuid4().hex
            if not self._claim(row["event_id"], claim_token,
                               row["request_id"], row["seq"]):
                return dispatched  # 前序未 dispatched 或行被并发持有 → 停止
            try:
                # 使用数据库 row["event_id"]（禁止按 request_id 重新拼接）
                self._publish(row["request_id"], row["event_type"],
                              row["payload"], row["event_id"])
            except Exception:
                self._release_claim(row["event_id"], claim_token)
                return dispatched  # 前序失败 → 禁止后序
            if not self._mark_dispatched(row["event_id"], claim_token):
                return dispatched  # 所有权丢失（被恢复）→ 前序未完成 → 停止
            dispatched += 1
        return dispatched

    def _publish(self, request_id: str, event_type: str, payload: dict,
                 event_id: str) -> None:
        """以稳定 outbox event_id 发布到 D1（相同 event_id 重复发布只产生一份事实）。"""
        d1 = self._d1()
        if event_type == "answer_ready":
            d1.publish_answer_event(
                request_id, payload.get("text", ""),
                payload.get("menu_ref", ""),
                payload.get("evidence_refs", []),
                event_id=event_id,
            )
        elif event_type == "result_committed":
            d1.publish_result_committed(
                request_id, payload.get("menu_summary", {}),
                event_id=event_id,
            )
        elif event_type == "clarification_needed":
            d1.publish_clarification_event(
                request_id, payload,
                event_id=event_id,
            )
        else:
            raise RuntimeError(f"unknown outbox event_type: {event_type}")


def dispatch_request(request_id: str) -> int:
    """便捷入口：发布指定请求的 success SSE（事务提交后调用）。"""
    return OutboxDispatcher().dispatch_request(request_id)


def dispatch_pending(request_id: str | None = None, limit: int = 100) -> int:
    """便捷入口：发布所有 pending/dispatching 事件（ADR-0006 重启补发）。

    用于 API 启动恢复：提交后进程崩溃或首次投递失败遗留的 pending 事件，
    在下次启动时补投，避免 success SSE 永久丢失。
    """
    return OutboxDispatcher().dispatch_pending(request_id, limit)
