"""T19 提交事务校验测试（原子性 / 幂等不可变 / 单调 fencing token / 结果与审计）。

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


def _audit(rid: str, plan_id: str, menu_hash: str = "a" * 64,
           verdict: str = "PASS") -> dict:
    """T19 完整健康审计（Artifact 链 + 引用）。"""
    return {
        "request_id": rid,
        "plan_id": plan_id,
        "recipe_ids": [1, 2],
        "menu_hash": menu_hash,
        "final_validation": {
            "ref": "fv:1", "request_id": rid, "plan_id": plan_id,
            "menu_hash": menu_hash, "recipe_ids": [1, 2],
            "verdict": verdict, "content_hash": "b" * 64,
        },
        "menu_decision": {
            "ref": "md:1", "request_id": rid, "plan_id": plan_id,
            "menu_hash": menu_hash, "content_hash": "c" * 64,
        },
        "review": {"ref": "rv:1", "request_id": rid, "status": "PASS",
                   "content_hash": "d" * 64},
        "answer": {
            "ref": "ans:1", "request_id": rid, "plan_id": plan_id,
            "menu_hash": menu_hash, "recipe_ids": [1, 2], "content_hash": "e" * 64,
        },
        "participant_constraint_refs": ["p1"],
        "ingredient_relation_coverage_refs": ["ev:1"],
        "override_refs": [],
        "tool_receipt_refs": ["tc:1"],
        "tool_input_output_hashes": [
            {"tool_call_id": "tc:1", "input_hash": "f" * 64, "output_hash": "1" * 64}],
        "final_validation_verdict": verdict,
    }


class TestAtomicCommit:
    def test_completed_commits_result_audit_and_outbox(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        result = commit_request_result(
            rid, sid, "completed", "plan-A", _audit(rid, "plan-A"),
            participant_refs=["p1"], fencing_token="10",
            answer_text="推荐菜单", menu_hash="a" * 64)
        assert result["committed"] is True
        assert not result["idempotent"]
        assert len(result["outbox_event_ids"]) == 2
        rows = _query("SELECT status, final_plan_id FROM recommendation_logs "
                      "WHERE request_id=%s", (rid,))
        assert rows and rows[0][0] == "completed" and rows[0][1] == "plan-A"
        ob = _query("SELECT event_type, seq FROM outbox WHERE request_id=%s ORDER BY seq",
                    (rid,))
        assert [r[0] for r in ob] == ["answer_ready", "result_committed"]
        assert [r[1] for r in ob] == [1, 2]

    def test_failed_commit_writes_no_outbox(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "failed", "", {"verdict": "EXCLUDE"},
                              participant_refs=["p1"])
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid,))[0][0] == 0

    def test_stale_fencing_token_rolls_back(self) -> None:
        sid = _unique("s")
        rid1 = _unique("r")
        commit_request_result(rid1, sid, "completed", "plan-A",
                              _audit(rid1, "plan-A"),
                              participant_refs=["p1"], fencing_token="100")
        rid2 = _unique("r")
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid2, sid, "completed", "plan-B",
                                  _audit(rid2, "plan-B"),
                                  participant_refs=["p1"], fencing_token="50")
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid2,))[0][0] == 0
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid2,))[0][0] == 0
        assert _query("SELECT final_plan_id FROM recommendation_logs WHERE request_id=%s",
                      (rid1,))[0][0] == "plan-A"

    def test_monotonic_token_accepted(self) -> None:
        sid = _unique("s")
        commit_request_result(_unique("r"), sid, "completed", "plan-A",
                              _audit(_unique("r"), "plan-A"),
                              participant_refs=["p1"], fencing_token="7")
        commit_request_result(_unique("r"), sid, "completed", "plan-B",
                              _audit(_unique("r"), "plan-B"),
                              participant_refs=["p1"], fencing_token="9")
        assert _query("SELECT fencing_token FROM sessions WHERE session_id=%s",
                      (sid,))[0][0] == 9

    def test_invalid_fencing_token_rejected(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        for bad in ("abc", "0", "-1", "no-lock-support"):
            with pytest.raises(AuditCommitFailed):
                commit_request_result(rid, sid, "completed", "plan-A",
                                      _audit(rid, "plan-A"),
                                      participant_refs=["p1"], fencing_token=bad)
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid,))[0][0] == 0

    def test_concurrent_connections_serialized_by_for_update(self) -> None:
        """两个真实 MySQL 连接并发竞争：较旧 token 无法晚提交覆盖较新 token。"""
        import threading

        sid = _unique("s")
        results: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def worker(token: str) -> None:
            rid = _unique("r")
            barrier.wait()
            try:
                commit_request_result(rid, sid, "completed", "plan-A",
                                      _audit(rid, "plan-A"),
                                      participant_refs=["p1"], fencing_token=token)
                results[token] = "ok"
            except AuditCommitFailed:
                results[token] = "rejected"

        threads = [threading.Thread(target=worker, args=(t,)) for t in ("5", "3")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        # 较新 token 无论顺序总是成功；较旧 token 若在较新之后提交必被拒绝
        assert results.get("5") == "ok"
        # 最终 stored token 是较新的 5（较旧 token 不能覆盖）
        assert _query("SELECT fencing_token FROM sessions WHERE session_id=%s",
                      (sid,))[0][0] == 5

    def test_idempotent_double_commit(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        r1 = commit_request_result(rid, sid, "completed", "plan-A",
                                   _audit(rid, "plan-A"),
                                   participant_refs=["p1"], fencing_token="10")
        r2 = commit_request_result(rid, sid, "completed", "plan-A",
                                   _audit(rid, "plan-A"),
                                   participant_refs=["p1"], fencing_token="10")
        assert r1["committed"] is True and not r1["idempotent"]
        assert r2["committed"] is True and r2["idempotent"] is True
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid,))[0][0] == 1
        assert _query("SELECT COUNT(*) FROM menu_versions WHERE session_id=%s",
                      (sid,))[0][0] == 1
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s",
                      (rid,))[0][0] == 2

    def test_same_request_different_payload_conflicts(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        with pytest.raises(AuditCommitFailed) as excinfo:
            commit_request_result(rid, sid, "completed", "plan-B",
                                  _audit(rid, "plan-B"),
                                  participant_refs=["p1"], fencing_token="10")
        assert "IDEMPOTENCY_CONFLICT" in str(excinfo.value)
        # 冲突不修改原结果
        assert _query("SELECT final_plan_id FROM recommendation_logs WHERE request_id=%s",
                      (rid,))[0][0] == "plan-A"


class TestArtifactAuditValidation:
    def test_missing_audit_field_fails_before_commit(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        audit = _audit(rid, "plan-A")
        del audit["final_validation"]
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid, sid, "completed", "plan-A", audit,
                                  participant_refs=["p1"], fencing_token="10")
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid,))[0][0] == 0
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid,))[0][0] == 0

    def test_non_pass_verdict_fails(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid, sid, "completed", "plan-A",
                                  _audit(rid, "plan-A", verdict="EXCLUDE"),
                                  participant_refs=["p1"], fencing_token="10")

    def test_menu_hash_mismatch_fails(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid, sid, "completed", "plan-A",
                                  _audit(rid, "plan-A", menu_hash="1" * 64),
                                  participant_refs=["p1"], fencing_token="10",
                                  menu_hash="2" * 64)
