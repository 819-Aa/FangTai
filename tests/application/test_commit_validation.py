"""T19 提交事务校验测试（原子性 / 幂等不可变 / 单调 fencing token / 结果与审计）。

依赖 MySQL 可用（提交写入真实 MySQL）。
"""

import hashlib
import uuid

import pytest

from db.migrations.apply_migration import apply_migrations
from food_agent_v2.application import AuditCommitFailed, commit_request_result
from food_agent_v2.c4.mysql_repository import MySQLSessionMemorySource
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
        "build_id": "7" * 32,
        "plan_id": plan_id,
        "recipe_ids": [1, 2],
        "menu_items": [
            {"recipe_id": 1, "name": "菜品一"},
            {"recipe_id": 2, "name": "菜品二"},
        ],
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
    def test_v2_acceptance_rejects_stale_and_missing_execution_owner(self) -> None:
        apply_migrations()
        rid, sid = _unique("r"), _unique("s")
        source = MySQLSessionMemorySource()
        source.save_session(sid, ["p1"], "langgraph", "v2")
        key_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        assert source.claim_request_acceptance(key_hash, "payload_hash", rid, sid)[0] == "winner"
        old = source.claim_request_execution_v2(rid, "old", 60)
        assert old is not None
        source.cursor.execute("UPDATE request_acceptances SET execution_lease_until="
                              "DATE_SUB(CURRENT_TIMESTAMP, INTERVAL 1 SECOND) WHERE request_id=%s",
                              (rid,))
        new = source.claim_request_execution_v2(rid, "new", 60)
        assert new is not None and new["generation"] == old["generation"] + 1

        kwargs = dict(request_id=rid, session_id=sid, status="failed", final_plan_id="",
                      health_evidence={"verdict": "EXCLUDE"}, participant_refs=["p1"])
        with pytest.raises(AuditCommitFailed, match="EXECUTION_LEASE_INVALID"):
            commit_request_result(**kwargs)
        with pytest.raises(AuditCommitFailed, match="EXECUTION_LEASE_INVALID"):
            commit_request_result(**kwargs, execution_owner="old",
                                  execution_generation=old["generation"])
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s", (rid,))[0][0] == 0

        result = commit_request_result(**kwargs, execution_owner="new",
                                       execution_generation=new["generation"])
        assert result["status"] == "failed" and result["committed"] is True
        assert _query("SELECT status, execution_generation FROM request_acceptances "
                      "WHERE request_id=%s", (rid,))[0] == ("terminal", new["generation"])
        assert source.claim_request_execution_v2(rid, "third", 60) is None
        replay = commit_request_result(**kwargs, execution_owner="new",
                                       execution_generation=new["generation"])
        assert replay["idempotent"] is True
        with pytest.raises(AuditCommitFailed, match="EXECUTION_LEASE_INVALID"):
            commit_request_result(**kwargs, execution_owner="old",
                                  execution_generation=old["generation"])

    def test_v2_rejects_missing_transition_revision_and_active_completion(self) -> None:
        import pymysql

        sid = _unique("s")
        cfg = load_config()
        conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                               user=cfg.mysql.user, password=cfg.mysql.password,
                               database=cfg.mysql.database, charset="utf8mb4")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (session_id, participant_refs, request_count, "
                    "workflow_mode, clarification_protocol_version, active_fencing_token, fencing_token) "
                    "VALUES (%s, '[]', 0, 'langgraph', 'v2', 10, 5)", (sid,),
                )
            conn.commit()
        finally:
            conn.close()

        rid = _unique("r")
        with pytest.raises(AuditCommitFailed, match="CLARIFICATION_TRANSITION_REQUIRED"):
            commit_request_result(rid, sid, "needs_clarification", "", {},
                                  participant_refs=["p1"], fencing_token="10")
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s", (rid,))[0][0] == 0

        with pytest.raises(AuditCommitFailed, match="CLARIFICATION_REVISION_REQUIRED"):
            commit_request_result(rid, sid, "needs_clarification", "", {},
                                  participant_refs=["p1"], fencing_token="10",
                                  clarification_transition={"next_question_id": _unique("q")})

        qid = _unique("q")
        conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                               user=cfg.mysql.user, password=cfg.mysql.password,
                               database=cfg.mysql.database, charset="utf8mb4")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clarification_questions "
                    "(question_id, session_id, producer_request_id, public_payload, private_snapshot) "
                    "VALUES (%s, %s, %s, '{}', '{}')", (qid, sid, _unique("r")),
                )
                cur.execute("UPDATE sessions SET active_clarification_question_id=%s WHERE session_id=%s",
                            (qid, sid))
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(AuditCommitFailed, match="ACTIVE_CLARIFICATION_UNRESOLVED"):
            commit_request_result(rid, sid, "completed", "plan-A", _audit(rid, "plan-A"),
                                  participant_refs=["p1"], fencing_token="10")

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

    def test_followup_clarification_marks_old_question_without_menu(self) -> None:
        from food_agent_v2.c4.mysql_repository import MySQLSessionMemorySource

        rid, sid, qid = _unique("r"), _unique("s"), _unique("q")
        result = commit_request_result(
            rid, sid, "needs_clarification", "",
            {"request_id": rid, "accepted_clarification_question_id": qid},
            participant_refs=["p1"],
        )

        source = MySQLSessionMemorySource()
        assert result["committed"] is True
        assert source.is_clarification_committed(sid, qid) is True
        assert source.is_clarification_committed(sid, _unique("q")) is False
        assert _query("SELECT COUNT(*) FROM menu_versions WHERE session_id=%s", (sid,))[0][0] == 0
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
        rid1 = _unique("r")
        commit_request_result(rid1, sid, "completed", "plan-A",
                              _audit(rid1, "plan-A"),
                              participant_refs=["p1"], fencing_token="7")
        rid2 = _unique("r")
        commit_request_result(rid2, sid, "completed", "plan-B",
                              _audit(rid2, "plan-B"),
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


class TestOuterBinding:
    def test_outer_request_id_mismatch_rejected(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        other = _unique("x")
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid, sid, "completed", "plan-A",
                                  _audit(other, "plan-A"),
                                  participant_refs=["p1"], fencing_token="10")
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid,))[0][0] == 0

    def test_review_fail_rejected(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        audit = _audit(rid, "plan-A")
        audit["review"]["status"] = "REVISION_REQUIRED"
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid, sid, "completed", "plan-A", audit,
                                  participant_refs=["p1"], fencing_token="10")

    def test_completed_without_token_rejected(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid, sid, "completed", "plan-A",
                                  _audit(rid, "plan-A"),
                                  participant_refs=["p1"], fencing_token=None)

    def test_changed_audit_hash_conflicts(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        # review content_hash 变化 → 不同信封 → IDEMPOTENCY_CONFLICT
        audit2 = _audit(rid, "plan-A")
        audit2["review"]["content_hash"] = "9" * 64
        with pytest.raises(AuditCommitFailed) as excinfo:
            commit_request_result(rid, sid, "completed", "plan-A", audit2,
                                  participant_refs=["p1"], fencing_token="10")
        assert "IDEMPOTENCY_CONFLICT" in str(excinfo.value)

    def test_changed_participant_refs_conflicts(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        audit2 = _audit(rid, "plan-A")
        audit2["participant_constraint_refs"] = ["p2"]
        with pytest.raises(AuditCommitFailed) as excinfo:
            commit_request_result(rid, sid, "completed", "plan-A", audit2,
                                  participant_refs=["p2"], fencing_token="10")
        assert "IDEMPOTENCY_CONFLICT" in str(excinfo.value)

    def test_menu_versions_hash_persisted(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              participant_refs=["p1"], fencing_token="10",
                              menu_hash="a" * 64)
        rows = _query("SELECT menu_hash, plan_id FROM menu_versions WHERE session_id=%s",
                      (sid,))
        assert rows and rows[0][0] == "a" * 64 and rows[0][1] == "plan-A"
        # outbox payload menu_hash 与审计一致
        ob = _query("SELECT payload FROM outbox WHERE request_id=%s AND event_type='result_committed'",
                    (rid,))
        import json as _json
        payload = _json.loads(ob[0][0])
        assert payload["menu_summary"]["build_id"] == "7" * 32
        assert payload["menu_summary"]["menu_hash"] == "a" * 64
        assert payload["menu_summary"]["items"] == [
            {"recipe_id": 1, "name": "菜品一"},
            {"recipe_id": 2, "name": "菜品二"},
        ]

    def test_audit_write_failure_rolls_back_all(self) -> None:
        """事务中间故障（outbox 写入前抛错）→ 结果/会话/菜单/outbox 全部回滚。"""
        import food_agent_v2.application.commit_service as CS
        rid, sid = _unique("r"), _unique("s")
        orig_build = CS._build_outbox_rows
        def boom(*a, **k):
            raise RuntimeError("injected mid-transaction failure")
        CS._build_outbox_rows = boom
        try:
            with pytest.raises(AuditCommitFailed):
                commit_request_result(rid, sid, "completed", "plan-A",
                                      _audit(rid, "plan-A"),
                                      participant_refs=["p1"], fencing_token="10")
        finally:
            CS._build_outbox_rows = orig_build
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid,))[0][0] == 0
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid,))[0][0] == 0
        assert _query("SELECT COUNT(*) FROM menu_versions WHERE session_id=%s",
                      (sid,))[0][0] == 0
        assert _query("SELECT COUNT(*) FROM sessions WHERE session_id=%s", (sid,))[0][0] == 0

    def test_changed_session_id_conflicts(self) -> None:
        """同 request 换 session → commit_hash 信封变化 → IDEMPOTENCY_CONFLICT。"""
        rid = _unique("r")
        commit_request_result(rid, _unique("s1"), "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        with pytest.raises(AuditCommitFailed) as excinfo:
            commit_request_result(rid, _unique("s2"), "completed", "plan-A",
                                  _audit(rid, "plan-A"),
                                  participant_refs=["p1"], fencing_token="10")
        assert "IDEMPOTENCY_CONFLICT" in str(excinfo.value)

    def test_changed_menu_ref_conflicts(self) -> None:
        """同 request 换 menu_ref → 信封变化 → IDEMPOTENCY_CONFLICT。"""
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              participant_refs=["p1"], fencing_token="10",
                              menu_ref="menu:1")
        with pytest.raises(AuditCommitFailed) as excinfo:
            commit_request_result(rid, sid, "completed", "plan-A",
                                  _audit(rid, "plan-A"),
                                  participant_refs=["p1"], fencing_token="10",
                                  menu_ref="menu:2")
        assert "IDEMPOTENCY_CONFLICT" in str(excinfo.value)

    @pytest.mark.parametrize("block", ["final_validation", "menu_decision", "answer"])
    def test_changed_artifact_hash_conflicts(self, block) -> None:
        """任意 Artifact hash 变化（FinalValidation/MenuDecision/Answer）→ 冲突。"""
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        audit2 = _audit(rid, "plan-A")
        if block == "final_validation":
            audit2["final_validation"]["content_hash"] = "9" * 64
        elif block == "menu_decision":
            audit2["menu_decision"]["content_hash"] = "9" * 64
        else:
            audit2["answer"]["content_hash"] = "9" * 64
        with pytest.raises(AuditCommitFailed) as excinfo:
            commit_request_result(rid, sid, "completed", "plan-A", audit2,
                                  participant_refs=["p1"], fencing_token="10")
        assert "IDEMPOTENCY_CONFLICT" in str(excinfo.value)

    def test_changed_artifact_and_tool_refs_conflicts(self) -> None:
        """Artifact ref / tool_call_id 变化（审计仍一致）→ 信封变化 → 冲突。"""
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        audit2 = _audit(rid, "plan-A")
        audit2["final_validation"]["ref"] = "fv:changed"
        audit2["menu_decision"]["ref"] = "md:changed"
        audit2["review"]["ref"] = "rv:changed"
        audit2["answer"]["ref"] = "ans:changed"
        # tool 回执与 hash 引用保持一一对应（校验通过），但 call_id 已变 → 信封不同
        audit2["tool_receipt_refs"] = ["tc:changed"]
        audit2["tool_input_output_hashes"] = [
            {"tool_call_id": "tc:changed", "input_hash": "f" * 64, "output_hash": "1" * 64}]
        with pytest.raises(AuditCommitFailed) as excinfo:
            commit_request_result(rid, sid, "completed", "plan-A", audit2,
                                  participant_refs=["p1"], fencing_token="10")
        assert "IDEMPOTENCY_CONFLICT" in str(excinfo.value)

    def test_missing_outer_menu_hash_derives_audit(self) -> None:
        """completed 未传 menu_hash → 派生自审计并持久化（绝不写空串）。"""
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A", menu_hash="a" * 64),
                              participant_refs=["p1"], fencing_token="10")
        rows = _query("SELECT menu_hash FROM menu_versions WHERE session_id=%s", (sid,))
        assert rows and rows[0][0] == "a" * 64
        import json as _json
        ob = _query("SELECT payload FROM outbox WHERE request_id=%s "
                    "AND event_type='result_committed'", (rid,))
        payload = _json.loads(ob[0][0])
        assert payload["menu_summary"]["menu_hash"] == "a" * 64

    def test_missing_outer_participant_refs_derives_audit(self) -> None:
        """completed 未传 participant_refs → 派生自审计并持久化（绝不写空列表）。"""
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              fencing_token="10")
        rows = _query("SELECT participant_refs FROM sessions WHERE session_id=%s", (sid,))
        assert rows
        import json as _json
        stored = _json.loads(rows[0][0])
        assert stored == ["p1"]


class TestCommittedSessionFacts:
    """会话事实（committed session facts）：fencing token / participant / current_menu 原子一致性。"""

    def test_failed_request_preserves_fencing_token(self) -> None:
        """completed token=100 → failed 不得清空 token → stale token=50 必须被拒绝。"""
        sid = _unique("s")
        rid1 = _unique("r")
        commit_request_result(rid1, sid, "completed", "plan-A",
                              _audit(rid1, "plan-A"),
                              participant_refs=["p1"], fencing_token="100")
        commit_request_result(_unique("r"), sid, "failed", "",
                              {"verdict": "EXCLUDE"}, participant_refs=["p1"])
        assert _query("SELECT fencing_token FROM sessions WHERE session_id=%s",
                      (sid,))[0][0] == 100
        rid3 = _unique("r")
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid3, sid, "completed", "plan-B",
                                  _audit(rid3, "plan-B"),
                                  participant_refs=["p1"], fencing_token="50")
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid3,))[0][0] == 0

    def test_session_participant_refs_tracks_latest_audit(self) -> None:
        """同 session 合法提交新参与者 p2 后，sessions 与本次健康审计一致（不再保留旧 p1）。"""
        sid = _unique("s")
        rid1 = _unique("r")
        commit_request_result(rid1, sid, "completed", "plan-A",
                              _audit(rid1, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        rid2 = _unique("r")
        audit2 = _audit(rid2, "plan-B")
        audit2["participant_constraint_refs"] = ["p2"]
        commit_request_result(rid2, sid, "completed", "plan-B", audit2,
                              participant_refs=["p2"], fencing_token="11")
        import json as _json
        rows = _query("SELECT participant_refs FROM sessions WHERE session_id=%s", (sid,))
        assert _json.loads(rows[0][0]) == ["p2"]

    def test_failed_without_participant_refs_preserves_old(self) -> None:
        """failed 且 participant_refs=None 时不得把旧参与者清成 []。"""
        sid = _unique("s")
        rid1 = _unique("r")
        commit_request_result(rid1, sid, "completed", "plan-A",
                              _audit(rid1, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        commit_request_result(_unique("r"), sid, "failed", "",
                              {"verdict": "EXCLUDE"})
        import json as _json
        rows = _query("SELECT participant_refs FROM sessions WHERE session_id=%s", (sid,))
        assert _json.loads(rows[0][0]) == ["p1"]

    def test_completed_writes_current_menu_plan_id(self) -> None:
        """completed 提交必须同一事务设置 current_menu_plan_id=final_plan_id。"""
        rid, sid = _unique("r"), _unique("s")
        commit_request_result(rid, sid, "completed", "plan-A",
                              _audit(rid, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        assert _query("SELECT current_menu_plan_id FROM sessions WHERE session_id=%s",
                      (sid,))[0][0] == "plan-A"

    def test_failed_preserves_current_menu_plan_id(self) -> None:
        """completed 后再 failed，current_menu_plan_id 保持此前菜单。"""
        sid = _unique("s")
        rid1 = _unique("r")
        commit_request_result(rid1, sid, "completed", "plan-A",
                              _audit(rid1, "plan-A"),
                              participant_refs=["p1"], fencing_token="10")
        commit_request_result(_unique("r"), sid, "failed", "",
                              {"verdict": "EXCLUDE"}, participant_refs=["p1"])
        assert _query("SELECT current_menu_plan_id FROM sessions WHERE session_id=%s",
                      (sid,))[0][0] == "plan-A"

    def test_mid_transaction_failure_preserves_session_fields(self) -> None:
        """中途事务故障：fencing_token / participant_refs / current_menu_plan_id 全部回滚。"""
        import food_agent_v2.application.commit_service as CS
        sid = _unique("s")
        rid1 = _unique("r")
        commit_request_result(rid1, sid, "completed", "plan-A",
                              _audit(rid1, "plan-A"),
                              participant_refs=["p1"], fencing_token="100")
        rid2 = _unique("r")
        orig_build = CS._build_outbox_rows
        def boom(*a, **k):
            raise RuntimeError("injected mid-transaction failure")
        CS._build_outbox_rows = boom
        try:
            with pytest.raises(AuditCommitFailed):
                commit_request_result(rid2, sid, "completed", "plan-B",
                                      _audit(rid2, "plan-B"),
                                      participant_refs=["p2"], fencing_token="101")
        finally:
            CS._build_outbox_rows = orig_build
        import json as _json
        row = _query("SELECT fencing_token, participant_refs, current_menu_plan_id "
                     "FROM sessions WHERE session_id=%s", (sid,))[0]
        assert row[0] == 100
        assert _json.loads(row[1]) == ["p1"]
        assert row[2] == "plan-A"
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid2,))[0][0] == 0


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

    @pytest.mark.parametrize("mutator", [
        lambda audit: audit.update(menu_items=[]),
        lambda audit: audit.update(menu_items=[
            {"recipe_id": 2, "name": "菜品二"},
            {"recipe_id": 1, "name": "菜品一"},
        ]),
        lambda audit: audit.update(build_id=""),
    ])
    def test_public_menu_projection_is_strictly_bound(self, mutator) -> None:
        rid, sid = _unique("r"), _unique("s")
        audit = _audit(rid, "plan-A")
        mutator(audit)
        with pytest.raises(AuditCommitFailed):
            commit_request_result(
                rid, sid, "completed", "plan-A", audit,
                participant_refs=["p1"], fencing_token="10",
            )
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s",
                      (rid,))[0][0] == 0

    def test_menu_hash_mismatch_fails(self) -> None:
        rid, sid = _unique("r"), _unique("s")
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid, sid, "completed", "plan-A",
                                  _audit(rid, "plan-A", menu_hash="1" * 64),
                                  participant_refs=["p1"], fencing_token="10",
                                  menu_hash="2" * 64)


class TestClarificationLifecycleCommit:
    def test_create_clarification_question(self) -> None:
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid, sid = _unique("r"), _unique("s")
        qid = _unique("q")

        pub = ClarificationPublicPayload(
            question_text="请问您偏好的辣度是？",
            options=[
                ClarificationOptionView(option_id=1, text="微辣"),
                ClarificationOptionView(option_id=2, text="重辣"),
            ],
            inquiry_category="spice_preference",
        )
        priv = ClarificationPrivateSnapshot(
            query_plan_snapshot={"intent": "spicy"},
            option_modifications={
                1: {"spice": "mild"},
                2: {"spice": "spicy"},
            },
            current_menu_version="v1",
        )

        transition = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )

        audit = _audit(rid, "clarification")
        commit_request_result(
            rid, sid, "needs_clarification", "clarification", audit,
            clarification_transition=transition,
        )

        sess_rows = _query(
            "SELECT active_clarification_question_id, clarification_revision FROM sessions WHERE session_id=%s",
            (sid,),
        )
        assert len(sess_rows) == 1
        assert sess_rows[0][0] == qid
        assert sess_rows[0][1] == 1

        q_rows = _query(
            "SELECT status, public_payload FROM clarification_questions WHERE question_id=%s",
            (qid,),
        )
        assert len(q_rows) == 1
        assert q_rows[0][0] == "pending"
        import json
        payload = json.loads(q_rows[0][1]) if isinstance(q_rows[0][1], str) else q_rows[0][1]
        assert payload["question_text"] == "请问您偏好的辣度是？"

        outbox_rows = _query(
            "SELECT event_type FROM outbox WHERE request_id=%s",
            (rid,),
        )
        assert any(r[0] == "clarification_needed" for r in outbox_rows)

    def test_consume_clarification_question(self) -> None:
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid1, sid = _unique("r"), _unique("s")
        qid = _unique("q")

        # Step 1: create question
        pub = ClarificationPublicPayload(
            question_text="测试问题",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv = ClarificationPrivateSnapshot(
            option_modifications={1: {"k": "v"}},
        )
        t1 = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid1, sid, "needs_clarification", "clarification", _audit(rid1, "c1"), clarification_transition=t1)

        # Step 2: consume question upon completion
        rid2 = _unique("r")
        t2 = ClarificationTransition(
            expected_question_id=qid,
            selected_option_id=1,
            expected_revision=1,
        )
        commit_request_result(
            rid2, sid, "completed", "plan-B", _audit(rid2, "plan-B"),
            participant_refs=["p1"], fencing_token="2",
            clarification_transition=t2,
        )

        # Active question should now be cleared
        sess_rows = _query(
            "SELECT active_clarification_question_id, clarification_revision FROM sessions WHERE session_id=%s",
            (sid,),
        )
        assert sess_rows[0][0] is None
        assert sess_rows[0][1] == 2

        q_rows = _query(
            "SELECT status, accepted_request_id, accepted_option_id FROM clarification_questions WHERE question_id=%s",
            (qid,),
        )
        assert q_rows[0][0] == "consumed"
        assert q_rows[0][1] == rid2
        assert q_rows[0][2] == 1

    def test_stale_question_rejection(self) -> None:
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid1, sid = _unique("r"), _unique("s")
        qid = _unique("q")

        pub = ClarificationPublicPayload(
            question_text="测试问题",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv = ClarificationPrivateSnapshot(
            option_modifications={1: {"k": "v"}},
        )
        t1 = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid1, sid, "needs_clarification", "clarification", _audit(rid1, "c1"), clarification_transition=t1)

        rid2 = _unique("r")
        stale_transition = ClarificationTransition(
            expected_question_id="wrong_question_id",
            selected_option_id=1,
        )
        with pytest.raises(AuditCommitFailed, match="CLARIFICATION_STALE"):
            commit_request_result(
                rid2, sid, "completed", "plan-B", _audit(rid2, "plan-B"),
                participant_refs=["p1"], fencing_token="2",
                clarification_transition=stale_transition,
            )

    def test_supersede_active_question(self) -> None:
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid1, sid = _unique("r"), _unique("s")
        qid1 = _unique("q")
        pub1 = ClarificationPublicPayload(
            question_text="问题1",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv1 = ClarificationPrivateSnapshot(option_modifications={1: {}})
        t1 = ClarificationTransition(
            next_question_id=qid1,
            next_public_payload=pub1.model_dump(),
            next_private_snapshot=priv1.model_dump(),
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid1, sid, "needs_clarification", "clarification", _audit(rid1, "c1"), clarification_transition=t1)

        rid2 = _unique("r")
        qid2 = _unique("q")
        pub2 = ClarificationPublicPayload(
            question_text="问题2",
            options=[ClarificationOptionView(option_id=2, text="选项2")],
        )
        priv2 = ClarificationPrivateSnapshot(option_modifications={2: {}})
        t2 = ClarificationTransition(
            supersede_current=True,
            next_question_id=qid2,
            next_public_payload=pub2.model_dump(),
            next_private_snapshot=priv2.model_dump(),
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid2, sid, "needs_clarification", "clarification", _audit(rid2, "c2"), clarification_transition=t2)

        sess_rows = _query(
            "SELECT active_clarification_question_id, clarification_revision FROM sessions WHERE session_id=%s",
            (sid,),
        )
        assert sess_rows[0][0] == qid2
        # Initial create -> rev=1; supersede -> rev=2; create next -> rev=3
        assert sess_rows[0][1] == 3

        q1_rows = _query("SELECT status FROM clarification_questions WHERE question_id=%s", (qid1,))
        assert q1_rows[0][0] == "superseded"

        q2_rows = _query("SELECT status FROM clarification_questions WHERE question_id=%s", (qid2,))
        assert q2_rows[0][0] == "pending"

    def test_idempotent_envelope_checked_first(self) -> None:
        """同一 request_id 重复提交命中信封幂等：不修改问题与会话事实，直接返回原结果。"""
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid, sid, qid = _unique("r"), _unique("s"), _unique("q")
        pub = ClarificationPublicPayload(
            question_text="幂等测试问题",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv = ClarificationPrivateSnapshot(option_modifications={1: {}})
        t = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )

        r1 = commit_request_result(
            rid, sid, "needs_clarification", "clarification", _audit(rid, "c1"),
            clarification_transition=t,
        )
        assert r1["committed"] is True
        assert r1["idempotent"] is False

        # 第二次相同信封提交
        r2 = commit_request_result(
            rid, sid, "needs_clarification", "clarification", _audit(rid, "c1"),
            clarification_transition=t,
        )
        assert r2["committed"] is True
        assert r2["idempotent"] is True

        # revision 仍然为 1，不因重试而递增
        sess_rows = _query("SELECT clarification_revision FROM sessions WHERE session_id=%s", (sid,))
        assert sess_rows[0][0] == 1

        # outbox 仅有 1 条
        ob_count = _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (rid,))[0][0]
        assert ob_count == 1

    def test_idempotent_different_payload_conflicts(self) -> None:
        """同一 request_id 用不同载荷提交：报错 IDEMPOTENCY_CONFLICT 且不改变原事实。"""
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid, sid, qid = _unique("r"), _unique("s"), _unique("q")
        pub1 = ClarificationPublicPayload(
            question_text="问题1",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        t1 = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub1.model_dump(),
            next_private_snapshot={},
            next_expires_at=1893456000.0,
        )
        commit_request_result(
            rid, sid, "needs_clarification", "clarification", _audit(rid, "c1"),
            clarification_transition=t1,
        )

        # 尝试使用不同 payload 再次提交同一 request_id
        pub2 = ClarificationPublicPayload(
            question_text="问题2不同内容",
            options=[ClarificationOptionView(option_id=2, text="选项2")],
        )
        t2 = ClarificationTransition(
            next_question_id=_unique("q2"),
            next_public_payload=pub2.model_dump(),
            next_private_snapshot={},
            next_expires_at=1893456000.0,
        )
        with pytest.raises(AuditCommitFailed, match="IDEMPOTENCY_CONFLICT"):
            commit_request_result(
                rid, sid, "needs_clarification", "clarification", _audit(rid, "c1"),
                clarification_transition=t2,
            )

    def test_nonexistent_option_rejected(self) -> None:
        """从锁定的问题私有选项映射复核 selected_option_id：不存在的选项回滚并抛出 CLARIFICATION_OPTION_NOT_FOUND。"""
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid1, sid, qid = _unique("r"), _unique("s"), _unique("q")
        pub = ClarificationPublicPayload(
            question_text="选项检验问题",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv = ClarificationPrivateSnapshot(option_modifications={1: {"k": "v"}})
        t1 = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid1, sid, "needs_clarification", "c1", _audit(rid1, "c1"), clarification_transition=t1)

        # 尝试消费不存在的 option_id=99
        rid2 = _unique("r")
        t2 = ClarificationTransition(
            expected_question_id=qid,
            selected_option_id=99,
        )
        with pytest.raises(AuditCommitFailed, match="CLARIFICATION_OPTION_NOT_FOUND"):
            commit_request_result(
                rid2, sid, "completed", "plan-B", _audit(rid2, "plan-B"),
                participant_refs=["p1"], fencing_token="2",
                clarification_transition=t2,
            )

        # 问题状态依然是 pending，revision 依然是 1
        q_rows = _query("SELECT status FROM clarification_questions WHERE question_id=%s", (qid,))
        assert q_rows[0][0] == "pending"
        sess_rows = _query("SELECT clarification_revision FROM sessions WHERE session_id=%s", (sid,))
        assert sess_rows[0][0] == 1

    def test_expired_question_rejected(self) -> None:
        """数据库到期时间检查：已过期的问题拒绝消费并抛出 CLARIFICATION_EXPIRED。"""
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid1, sid, qid = _unique("r"), _unique("s"), _unique("q")
        pub = ClarificationPublicPayload(
            question_text="过期测试问题",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv = ClarificationPrivateSnapshot(option_modifications={1: {}})
        # 设置过期时间为 1970 年
        t1 = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=100.0,
        )
        commit_request_result(rid1, sid, "needs_clarification", "c1", _audit(rid1, "c1"), clarification_transition=t1)

        # 尝试消费已过期的问题
        rid2 = _unique("r")
        t2 = ClarificationTransition(
            expected_question_id=qid,
            selected_option_id=1,
        )
        with pytest.raises(AuditCommitFailed, match="CLARIFICATION_EXPIRED"):
            commit_request_result(
                rid2, sid, "completed", "plan-B", _audit(rid2, "plan-B"),
                participant_refs=["p1"], fencing_token="2",
                clarification_transition=t2,
            )

    def test_revision_mismatch_rejected(self) -> None:
        """expected_revision 与 sessions.clarification_revision 不一致时拒绝提交。"""
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid1, sid, qid = _unique("r"), _unique("s"), _unique("q")
        pub = ClarificationPublicPayload(
            question_text="版本校验问题",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv = ClarificationPrivateSnapshot(option_modifications={1: {}})
        t1 = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid1, sid, "needs_clarification", "c1", _audit(rid1, "c1"), clarification_transition=t1)

        # 当前 rev=1，传入 expected_revision=99
        rid2 = _unique("r")
        t2 = ClarificationTransition(
            expected_question_id=qid,
            selected_option_id=1,
            expected_revision=99,
        )
        with pytest.raises(AuditCommitFailed, match="CLARIFICATION_REVISION_MISMATCH"):
            commit_request_result(
                rid2, sid, "completed", "plan-B", _audit(rid2, "plan-B"),
                participant_refs=["p1"], fencing_token="2",
                clarification_transition=t2,
            )

    def test_v2_fencing_token_validation(self) -> None:
        """v2 事务下 fencing_token 必须等于 sessions.active_fencing_token 且大于 sessions.fencing_token。"""
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        sid = _unique("s")
        # 预先设置 v2 会话：active_fencing_token=10，已提交 fencing_token=5
        import pymysql
        cfg = load_config()
        conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port,
                               user=cfg.mysql.user, password=cfg.mysql.password,
                               database=cfg.mysql.database, charset="utf8mb4", autocommit=True)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO sessions
            (session_id, participant_refs, request_count, workflow_mode, clarification_protocol_version, active_fencing_token, fencing_token)
            VALUES (%s, '[]', 0, 'langgraph', 'v2', 10, 5)
            """,
            (sid,),
        )
        conn.close()

        rid = _unique("r")
        qid = _unique("q")
        pub = ClarificationPublicPayload(
            question_text="v2 fencing 测试",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv = ClarificationPrivateSnapshot(option_modifications={1: {}})
        t = ClarificationTransition(
            next_question_id=qid,
            expected_revision=0,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )

        # 1. 传入空 fencing_token
        with pytest.raises(AuditCommitFailed):
            commit_request_result(rid, sid, "needs_clarification", "c1", _audit(rid, "c1"), clarification_transition=t)

        # 2. 传入与 active_fencing_token 不一致的 token (8 != 10)
        with pytest.raises(AuditCommitFailed, match="FENCING_TOKEN_MISMATCH"):
            commit_request_result(rid, sid, "needs_clarification", "c1", _audit(rid, "c1"),
                                  fencing_token="8", clarification_transition=t)

        # 3. 传入匹配的 active_fencing_token 10（大于 5）
        result = commit_request_result(rid, sid, "needs_clarification", "c1", _audit(rid, "c1"),
                                      fencing_token="10", clarification_transition=t)
        assert result["committed"] is True

        # 4. sessions.fencing_token 已更新为 10
        sess_rows = _query("SELECT fencing_token FROM sessions WHERE session_id=%s", (sid,))
        assert sess_rows[0][0] == 10

    def test_concurrent_competition_single_winner(self) -> None:
        """两个并发请求竞争消费同一活跃问题：仅一个提交成功，失败请求回滚且无业务副作用。"""
        import threading

        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid1, sid, qid = _unique("r"), _unique("s"), _unique("q")
        pub = ClarificationPublicPayload(
            question_text="并发竞争问题",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv = ClarificationPrivateSnapshot(option_modifications={1: {}})
        t1 = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid1, sid, "needs_clarification", "c1", _audit(rid1, "c1"), clarification_transition=t1)

        rid_a, rid_b = _unique("ra"), _unique("rb")
        results = {}
        barrier = threading.Barrier(2)

        def worker(rid: str, token: str) -> None:
            barrier.wait()
            t = ClarificationTransition(expected_question_id=qid, selected_option_id=1)
            try:
                r = commit_request_result(
                    rid, sid, "completed", f"plan-{rid}", _audit(rid, f"plan-{rid}"),
                    participant_refs=["p1"], fencing_token=token, clarification_transition=t,
                )
                results[rid] = ("success", r)
            except AuditCommitFailed as e:
                results[rid] = ("failed", str(e))

        t_a = threading.Thread(target=worker, args=(rid_a, "10"))
        t_b = threading.Thread(target=worker, args=(rid_b, "11"))
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        successes = [k for k, v in results.items() if v[0] == "success"]
        failures = [k for k, v in results.items() if v[0] == "failed"]
        assert len(successes) == 1
        assert len(failures) == 1

        failed_rid = failures[0]
        # 失败请求不能生成菜单版本、不可变日志或 outbox 行
        assert _query("SELECT COUNT(*) FROM recommendation_logs WHERE request_id=%s", (failed_rid,))[0][0] == 0
        assert _query("SELECT COUNT(*) FROM menu_versions WHERE plan_id=%s", (f"plan-{failed_rid}",))[0][0] == 0
        assert _query("SELECT COUNT(*) FROM outbox WHERE request_id=%s", (failed_rid,))[0][0] == 0

    def test_failed_status_does_not_mutate_clarification(self) -> None:
        """失败、中断、取消的请求不得消费或替代现有问题，也不递增 revision。"""
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )

        rid1, sid, qid = _unique("r"), _unique("s"), _unique("q")
        pub = ClarificationPublicPayload(
            question_text="测试问题",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        priv = ClarificationPrivateSnapshot(option_modifications={1: {}})
        t1 = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid1, sid, "needs_clarification", "c1", _audit(rid1, "c1"), clarification_transition=t1)

        # 尝试以 failed 提交带有消费转移的请求
        rid2 = _unique("r")
        t2 = ClarificationTransition(expected_question_id=qid, selected_option_id=1)
        commit_request_result(
            rid2, sid, "failed", "", {"verdict": "FAIL"},
            clarification_transition=t2,
        )

        # 问题依然是 pending，active 依然是 qid，revision 依然是 1
        q_rows = _query("SELECT status FROM clarification_questions WHERE question_id=%s", (qid,))
        assert q_rows[0][0] == "pending"
        sess_rows = _query("SELECT active_clarification_question_id, clarification_revision FROM sessions WHERE session_id=%s", (sid,))
        assert sess_rows[0][0] == qid
        assert sess_rows[0][1] == 1


class TestClarificationOutboxDelivery:
    """Task 3: 澄清事件专享 Transactional Outbox 投递与恢复。"""

    def test_outbox_dispatches_clarification_event_with_stable_id(self) -> None:
        """clarification_needed 事件经 outbox 派送，带稳定 event_id 并标记 dispatched。"""
        from food_agent_v2.application.outbox import dispatch_request
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPrivateSnapshot,
            ClarificationPublicPayload,
            ClarificationTransition,
        )
        from food_agent_v2.d1 import api as d1_api

        rid, sid, qid = _unique("r"), _unique("s"), _unique("q")
        d1_api._requests[rid] = {"request_id": rid, "status": "running", "session_id": sid, "stage_events_cursor": 0}

        pub = ClarificationPublicPayload(
            question_text="您想要几道菜？",
            options=[ClarificationOptionView(option_id=1, text="2道菜"), ClarificationOptionView(option_id=2, text="3道菜")],
            inquiry_category="dish_count",
        )
        priv = ClarificationPrivateSnapshot(option_modifications={1: {"dish_count": 2}, 2: {"dish_count": 3}})
        t = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot=priv.model_dump(),
            next_expires_at=1893456000.0,
        )

        commit_request_result(
            rid, sid, "needs_clarification", "c1", _audit(rid, "c1"),
            clarification_transition=t,
        )

        # 事务提交后，outbox 应有 pending 记录
        ob_rows = _query("SELECT status, event_id FROM outbox WHERE request_id=%s", (rid,))
        assert len(ob_rows) == 1
        assert ob_rows[0][0] == "pending"
        expected_eid = f"ev_clarify_{rid}"
        assert ob_rows[0][1] == expected_eid

        # 执行派送
        dispatched_count = dispatch_request(rid)
        assert dispatched_count == 1

        # 检查 outbox 状态已更新为 dispatched
        ob_status = _query("SELECT status FROM outbox WHERE event_id=%s", (expected_eid,))
        assert ob_status[0][0] == "dispatched"

        # 检查 D1 事件流
        events = d1_api._events.get(rid, [])
        assert len(events) >= 1
        clarify_event = [e for e in events if e.get("id") == expected_eid]
        assert len(clarify_event) == 1
        assert clarify_event[0]["event"] == "clarification_needed"
        import json
        data = json.loads(clarify_event[0]["data"])
        assert data["question_id"] == qid
        assert len(data["options"]) == 2

    def test_repeated_dispatch_no_duplicate_sse_cursor(self) -> None:
        """重复派送已 dispatched 的澄清事件不重复写入，不递增 SSE 游标。"""
        from food_agent_v2.application.outbox import dispatch_request
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPublicPayload,
            ClarificationTransition,
        )
        from food_agent_v2.d1 import api as d1_api

        rid, sid, qid = _unique("r"), _unique("s"), _unique("q")
        d1_api._requests[rid] = {"request_id": rid, "status": "running", "session_id": sid, "stage_events_cursor": 0}

        pub = ClarificationPublicPayload(
            question_text="幂等游标测试",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        t = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot={},
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid, sid, "needs_clarification", "c1", _audit(rid, "c1"), clarification_transition=t)

        assert dispatch_request(rid) == 1
        initial_cursor = d1_api._requests[rid]["stage_events_cursor"]
        events_len = len(d1_api._events.get(rid, []))

        # 再次执行派送
        assert dispatch_request(rid) == 0
        assert d1_api._requests[rid]["stage_events_cursor"] == initial_cursor
        assert len(d1_api._events.get(rid, [])) == events_len

    def test_crash_recovery_via_dispatch_pending(self) -> None:
        """模拟 commit 后、派送前进程崩溃：恢复循环 dispatch_pending 能够补发。"""
        from food_agent_v2.application.outbox import dispatch_pending
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPublicPayload,
            ClarificationTransition,
        )
        from food_agent_v2.d1 import api as d1_api

        rid, sid, qid = _unique("r"), _unique("s"), _unique("q")
        d1_api._requests[rid] = {"request_id": rid, "status": "running", "session_id": sid, "stage_events_cursor": 0}

        pub = ClarificationPublicPayload(
            question_text="崩溃恢复测试",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        t = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot={},
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid, sid, "needs_clarification", "c1", _audit(rid, "c1"), clarification_transition=t)

        # 此时未调用 dispatch_request，D1 中尚无 clarification 事件
        events = d1_api._events.get(rid, [])
        assert not any(e.get("event") == "clarification_needed" for e in events)

        # 模拟重启后的恢复循环
        total = dispatch_pending(rid)
        assert total == 1
        events = d1_api._events.get(rid, [])
        assert any(e.get("event") == "clarification_needed" for e in events)
        assert _query("SELECT status FROM outbox WHERE request_id=%s", (rid,))[0][0] == "dispatched"

    def test_dispatch_failure_retains_pending(self) -> None:
        """投递失败时释放 claim 并保留 pending，后续可重试。"""
        from unittest.mock import patch

        from food_agent_v2.application.outbox import OutboxDispatcher
        from food_agent_v2.contracts.clarification import (
            ClarificationOptionView,
            ClarificationPublicPayload,
            ClarificationTransition,
        )
        from food_agent_v2.d1 import api as d1_api

        rid, sid, qid = _unique("r"), _unique("s"), _unique("q")
        d1_api._requests[rid] = {"request_id": rid, "status": "running", "session_id": sid, "stage_events_cursor": 0}

        pub = ClarificationPublicPayload(
            question_text="失败重试测试",
            options=[ClarificationOptionView(option_id=1, text="选项1")],
        )
        t = ClarificationTransition(
            next_question_id=qid,
            next_public_payload=pub.model_dump(),
            next_private_snapshot={},
            next_expires_at=1893456000.0,
        )
        commit_request_result(rid, sid, "needs_clarification", "c1", _audit(rid, "c1"), clarification_transition=t)

        dispatcher = OutboxDispatcher(d1_api=d1_api)

        # 模拟 D1 发生临时异常
        with patch.object(d1_api, "publish_clarification_event", side_effect=RuntimeError("Redis connection lost")):
            dispatched = dispatcher.dispatch_request(rid)
            assert dispatched == 0

        # 行状态必须恢复为 pending，claim_token 被清空
        row = _query("SELECT status, claim_token FROM outbox WHERE request_id=%s", (rid,))[0]
        assert row[0] == "pending"
        assert row[1] is None

        # 恢复后重试应成功
        dispatched_retry = dispatcher.dispatch_request(rid)
        assert dispatched_retry == 1
        assert _query("SELECT status FROM outbox WHERE request_id=%s", (rid,))[0][0] == "dispatched"
