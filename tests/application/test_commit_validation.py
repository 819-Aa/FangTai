"""T19 提交事务校验测试（原子性 / 单调 fencing token / 结果与审计）。

依赖 MySQL 可用（提交写入真实 MySQL）。
"""

import uuid

import pytest

from food_agent_v2.application import AuditCommitFailed, commit_request_result
from food_agent_v2.core.config import load_config


def _mysql_available() -> bool:
    try:
        import pymysql

        cfg = load_config()
        conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                               user=cfg.mysql.user, password=cfg.mysql.password,
                               database=cfg.mysql.database, charset="utf8mb4",
                               connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mysql_available(), reason="MySQL 不可用")


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _query(sql: str, params=()):
    import pymysql

    cfg = load_config()
    conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                           user=cfg.mysql.user, password=cfg.mysql.password,
                           database=cfg.mysql.database, charset="utf8mb4")
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


class TestAtomicCommit:
    def test_completed_commits_result_audit_and_outbox(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        result = commit_request_result(
            rid, sid, "completed", "plan-A",
            {"recipe_ids": [1, 2], "verdict": "PASS"},
            participant_refs=["p1"], fencing_token="10",
            answer_text="推荐菜单", menu_hash="a" * 64)
        assert result["committed"] is True
        assert len(result["outbox_event_ids"]) == 2
        # 结果 + 审计已写
        rows = _query("SELECT status, final_plan_id FROM recommendation_logs "
                      "WHERE request_id=%s", (rid,))
        assert rows and rows[0][0] == "completed" and rows[0][1] == "plan-A"
        # outbox 两行 pending
        ob = _query("SELECT event_type, seq FROM outbox WHERE request_id=%s ORDER BY seq",
                    (rid,))
        assert [r[0] for r in ob] == ["answer_ready", "result_committed"]
        assert [r[1] for r in ob] == [1, 2]

    def test_failed_commit_writes_no_outbox(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "failed", "", {"verdict": "EXCLUDE"},
                              participant_refs=["p1"])
        # 失败请求无 outbox 行（success SSE 仅 completed）
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid,))[0][0] == 0

    def test_stale_fencing_token_rolls_back(self) -> None:
        sid = _unique("s")
        rid1 = _unique("r")
        commit_request_result(rid1, sid, "completed", "plan-A",
                              {"recipe_ids": [1], "verdict": "PASS"},
                              participant_refs=["p1"], fencing_token="100")
        # 同 session 用更小 token → stale → 回滚
        rid2 = _unique("r")
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid2, sid, "completed", "plan-B",
                                  {"recipe_ids": [2], "verdict": "PASS"},
                                  participant_refs=["p1"], fencing_token="50")
        # 回滚 → plan-B 的结果与 outbox 均不存在
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid2,))[0][0] == 0
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid2,))[0][0] == 0
        # 先前 plan-A 提交仍完整
        assert _query("SELECT final_plan_id FROM recommendation_logs WHERE request_id=%s",
                      (rid1,))[0][0] == "plan-A"

    def test_monotonic_token_accepted(self) -> None:
        sid = _unique("s")
        commit_request_result(_unique("r"), sid, "completed", "plan-A",
                              {"recipe_ids": [1], "verdict": "PASS"},
                              participant_refs=["p1"], fencing_token="7")
        # 递增 token 接受
        commit_request_result(_unique("r"), sid, "completed", "plan-B",
                              {"recipe_ids": [2], "verdict": "PASS"},
                              participant_refs=["p1"], fencing_token="9")
        assert _query("SELECT fencing_token FROM sessions WHERE session_id=%s",
                      (sid,))[0][0] == "9"

    def test_invalid_fencing_token_rejected(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid, sid, "completed", "plan-A",
                                  {"recipe_ids": [1], "verdict": "PASS"},
                                  participant_refs=["p1"], fencing_token="abc")
        # 校验在写入前失败 → 无结果/无 outbox
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid,))[0][0] == 0
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid,))[0][0] == 0

    def test_idempotent_double_commit(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        for _ in range(2):
            commit_request_result(rid, sid, "completed", "plan-A",
                                  {"recipe_ids": [1], "verdict": "PASS"},
                                  participant_refs=["p1"])
        # 同一 request 只一条结果 + 两行 outbox（幂等）
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid,))[0][0] == 1
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid,))[0][0] == 2
