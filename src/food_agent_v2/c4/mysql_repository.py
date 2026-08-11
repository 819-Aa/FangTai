"""C4 MySQL 会话记忆 Repository（T18）—— 已提交边界的事实来源。

MySQL 保存永久/已提交会话事实（sessions / conversation_events / menu_versions），
Redis 只保存可恢复运行快照。本模块是 C4 读取 MySQL 已提交边界的唯一入口；
内存 Fake 仅供单元测试（真实实现仅在 MySQL 可用时使用）。
"""

from __future__ import annotations

import json
from typing import Protocol

from food_agent_v2.core.config import load_config


class SessionMemorySource(Protocol):
    """已提交会话记忆来源（MySQL 生产实现或测试 Fake）。"""

    def load_session(self, session_id: str) -> dict | None: ...
    def load_committed_events(self, session_id: str) -> list[dict]: ...
    def load_menu_versions(self, session_id: str) -> list[dict]: ...
    def save_session(self, session_id: str, participant_refs: list[str]) -> None: ...


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

    def save_session(self, session_id: str, participant_refs: list[str]) -> None:
        """持久化会话边界（INSERT IGNORE；供 C4 公开 create 接口）。"""
        self.cursor.execute(
            "INSERT IGNORE INTO sessions "
            "(session_id, participant_refs, request_count) VALUES (%s, %s, 0)",
            (session_id, json.dumps(participant_refs, ensure_ascii=False)))

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


class InMemorySessionMemorySource:
    """单元测试 Fake：内存中的已提交会话记忆。"""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.menus: dict[str, list[dict]] = {}

    def load_session(self, session_id: str) -> dict | None:
        meta = self.sessions.get(session_id)
        if meta is None:
            return None
        return dict(meta) | {"last_request_at": meta.get("last_request_at")}

    def load_committed_events(self, session_id: str) -> list[dict]:
        return list(self.events.get(session_id, []))

    def load_menu_versions(self, session_id: str) -> list[dict]:
        return list(self.menus.get(session_id, []))

    def save_session(self, session_id: str, participant_refs: list[str]) -> None:
        self.sessions[session_id] = {"session_id": session_id,
                                     "participant_refs": list(participant_refs),
                                     "current_menu_plan_id": None,
                                     "request_count": 0,
                                     "last_request_at": None}


def default_mysql_session_memory_source() -> MySQLSessionMemorySource:
    return MySQLSessionMemorySource()
