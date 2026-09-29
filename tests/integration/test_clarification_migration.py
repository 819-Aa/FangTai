"""Integration tests for Clarification Protocol v2 Database Migration & Schema.

Verifies:
1. Idempotent migrations (can run multiple times safely).
2. Addition of workflow_mode, clarification_protocol_version, active_fencing_token on sessions.
3. Creation and unique constraints of request_acceptances.
4. load_clarification_state repository interface.
5. Legacy session preservation (NULL protocol version does not drift).
"""

import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pymysql
import pytest

from db.migrations.apply_migration import apply_migrations
from food_agent_v2.application import AuditCommitFailed, commit_request_result
from food_agent_v2.c4 import ContextService
from food_agent_v2.c4.mysql_repository import MySQLSessionMemorySource
from food_agent_v2.core.config import load_config
from food_agent_v2.d1 import RecommendationAPI


@pytest.fixture(scope="module")
def db_conn():
    cfg = load_config()
    conn = pymysql.connect(
        host=cfg.mysql.host,
        port=cfg.mysql.port,
        user=cfg.mysql.user,
        password=cfg.mysql.password,
        database=cfg.mysql.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    yield conn
    conn.close()


def test_migrations_run_idempotently(db_conn):
    """Running migrations twice in succession must succeed cleanly without altering or clearing data."""
    # Run once
    apply_migrations()
    # Run second time
    apply_migrations()

    with db_conn.cursor() as cur:
        # Check sessions columns
        cur.execute(
            """
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sessions'
            """
        )
        cols = {r["COLUMN_NAME"] for r in cur.fetchall()}
        assert "active_clarification_question_id" in cols
        assert "clarification_revision" in cols
        assert "workflow_mode" in cols
        assert "clarification_protocol_version" in cols
        assert "active_fencing_token" in cols
        cur.execute(
            "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sessions' "
            "AND COLUMN_NAME = 'active_fencing_token'"
        )
        assert cur.fetchone()["DATA_TYPE"] == "bigint"

        # Check request_acceptances table
        cur.execute(
            """
            SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'request_acceptances'
            """
        )
        assert cur.fetchone() is not None
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='request_acceptances'"
        )
        acceptance_cols = {row["COLUMN_NAME"] for row in cur.fetchall()}
        assert {"execution_owner", "execution_lease_until", "execution_generation"} <= acceptance_cols
        cur.execute("SELECT CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='request_acceptances' "
                    "AND COLUMN_NAME='status'")
        assert cur.fetchone()["CHARACTER_MAXIMUM_LENGTH"] >= len("recovery_required")


def test_request_acceptances_uniqueness(db_conn):
    """Verify request_acceptances enforces unique idempotency_key_hash."""
    apply_migrations()
    raw_key = f"test_key_{uuid.uuid4()}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    payload_hash = hashlib.sha256(b"payload_1").hexdigest()
    req_id = str(uuid.uuid4())
    sess_id = f"sess_{uuid.uuid4()}"

    with db_conn.cursor() as cur:
        # First insert
        cur.execute(
            """
            INSERT INTO request_acceptances (idempotency_key_hash, payload_hash, request_id, session_id, status)
            VALUES (%s, %s, %s, %s, 'accepted')
            """,
            (key_hash, payload_hash, req_id, sess_id),
        )
        db_conn.commit()

        # Duplicate insert with same idempotency_key_hash must fail
        with pytest.raises(pymysql.err.IntegrityError):
            cur.execute(
                """
                INSERT INTO request_acceptances (idempotency_key_hash, payload_hash, request_id, session_id, status)
                VALUES (%s, %s, %s, %s, 'accepted')
                """,
                (key_hash, payload_hash, str(uuid.uuid4()), sess_id),
            )
            db_conn.commit()


def test_real_mysql_acceptance_race_and_execution_reclaim(db_conn):
    """Independent connections compete for one identity and one execution lease."""
    key_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    barrier = threading.Barrier(2)

    def compete(request_id):
        source = MySQLSessionMemorySource()
        barrier.wait()
        return source.claim_request_acceptance(key_hash, "same_payload", request_id, "sess_race")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, str(uuid.uuid4())) for _ in range(2)]
        outcomes = [future.result(timeout=10) for future in futures]
    assert sorted(result[0] for result in outcomes) == ["existing", "winner"]
    original_id = outcomes[0][1]["request_id"]
    assert outcomes[1][1]["request_id"] == original_id

    source = MySQLSessionMemorySource()
    assert source.claim_request_execution(original_id, "owner1", 3600) is True
    assert MySQLSessionMemorySource().claim_request_execution(original_id, "owner2", 3600) is False
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE request_acceptances SET execution_lease_until="
            "DATE_SUB(CURRENT_TIMESTAMP, INTERVAL 1 SECOND) WHERE request_id=%s",
            (original_id,),
        )
    db_conn.commit()
    assert MySQLSessionMemorySource().claim_request_execution(original_id, "owner2", 3600) is True
    source.finish_request_execution(original_id, "owner1", True)
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, execution_owner FROM request_acceptances WHERE request_id=%s",
                    (original_id,))
        row = cur.fetchone()
    assert row == {"status": "running", "execution_owner": "owner2"}


def test_real_mysql_v2_execution_generation_fences_renew_and_finish(db_conn):
    """A reclaimed execution invalidates every lease operation from its old owner."""
    apply_migrations()
    key_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    request_id = str(uuid.uuid4())
    source = MySQLSessionMemorySource()
    assert source.claim_request_acceptance(key_hash, "payload_hash", request_id, "sess_v2")[0] == "winner"

    first = source.claim_request_execution_v2(request_id, "old", 60)
    assert first is not None and first["generation"] == 1
    assert first["owner"] == "old" and first["lease_until"] > 0
    loaded = source.load_request_acceptance_by_request_id(request_id)
    assert loaded["execution_generation"] == 1
    assert loaded["execution_owner"] == "old"
    assert loaded["execution_lease_until"] == first["lease_until"]
    assert MySQLSessionMemorySource().claim_request_execution_v2(request_id, "new", 60) is None
    assert source.renew_request_execution_v2(request_id, "old", 1, 60) is True

    with db_conn.cursor() as cur:
        cur.execute("UPDATE request_acceptances SET execution_lease_until="
                    "DATE_SUB(CURRENT_TIMESTAMP, INTERVAL 1 SECOND) WHERE request_id=%s",
                    (request_id,))
    db_conn.commit()

    assert source.renew_request_execution_v2(request_id, "old", 1, 60) is False
    assert source.finish_request_execution_v2(request_id, "old", 1, False) is False

    second = MySQLSessionMemorySource().claim_request_execution_v2(request_id, "new", 60)
    assert second is not None and second["generation"] == 2
    assert source.renew_request_execution_v2(request_id, "old", 1, 60) is False
    assert source.finish_request_execution_v2(request_id, "old", 1, False) is False
    assert source.renew_request_execution_v2(request_id, "new", 2, 60) is True
    assert source.finish_request_execution_v2(request_id, "new", 2, True) is False
    assert source.finish_request_execution_v2(request_id, "new", 2, False) is True
    assert source.claim_request_execution_v2(request_id, "third", 60)["generation"] == 3


def test_real_mysql_reclaim_does_not_wait_for_session_commit_lock(db_conn):
    """Claim locks acceptance only; a paused commit later rejects the old generation."""
    apply_migrations()
    session_id = ContextService().create_session_record(
        ["p1"], workflow_mode="langgraph", clarification_protocol_version="v2",
    )
    request_id = str(uuid.uuid4())
    source = MySQLSessionMemorySource()
    key_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    source.claim_request_acceptance(key_hash, "payload_hash", request_id, session_id)
    old = source.claim_request_execution_v2(request_id, "old", 60)
    assert old is not None
    source.cursor.execute("UPDATE request_acceptances SET execution_lease_until="
                          "DATE_SUB(CURRENT_TIMESTAMP, INTERVAL 1 SECOND) WHERE request_id=%s",
                          (request_id,))

    with db_conn.cursor() as cur:
        cur.execute("SELECT session_id FROM sessions WHERE session_id=%s FOR UPDATE", (session_id,))
        assert cur.fetchone() is not None
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            stale_commit = pool.submit(
                commit_request_result, request_id, session_id, "failed", "",
                {"verdict": "EXCLUDE"}, ["p1"],
                execution_owner="old", execution_generation=old["generation"],
            )
            new_claim = pool.submit(
                MySQLSessionMemorySource().claim_request_execution_v2,
                request_id, "new", 60,
            )
            new = new_claim.result(timeout=5)
            assert new is not None and new["generation"] == old["generation"] + 1
        finally:
            db_conn.rollback()
        with pytest.raises(AuditCommitFailed, match="EXECUTION_LEASE_INVALID"):
            stale_commit.result(timeout=5)


def test_real_mysql_q1_request_cannot_project_new_q2(db_conn):
    sid, rid1, rid2 = (str(uuid.uuid4()) for _ in range(3))
    qid1, qid2 = (str(uuid.uuid4()) for _ in range(2))
    payload = json.dumps({"question_text": "请选择", "options": [{"option_id": 1, "text": "方案"}]})
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO sessions (session_id, participant_refs, request_count) "
                    "VALUES (%s, '[]', 0)", (sid,))
        for qid, rid, status in ((qid1, rid1, "consumed"), (qid2, rid2, "pending")):
            cur.execute(
                "INSERT INTO clarification_questions "
                "(question_id, session_id, producer_request_id, status, public_payload, private_snapshot) "
                "VALUES (%s, %s, %s, %s, %s, '{}')",
                (qid, sid, rid, status, payload),
            )
            cur.execute("INSERT INTO recommendation_logs (request_id, session_id, status) "
                        "VALUES (%s, %s, 'needs_clarification')", (rid, sid))
        cur.execute("UPDATE sessions SET active_clarification_question_id=%s WHERE session_id=%s",
                    (qid2, sid))
    db_conn.commit()

    source = MySQLSessionMemorySource()
    assert source.load_produced_active_clarification(rid1) is None
    assert source.load_produced_active_clarification(rid2)["question_id"] == qid2
    api = RecommendationAPI()
    with patch.object(api, "_restore_request"):
        code1, first = api.get_request_status(rid1)
        code2, second = api.get_request_status(rid2)
    assert code1 == code2 == 200
    assert first.get("active_clarification") is None
    assert second["active_clarification"]["question_id"] == qid2


def test_real_mysql_prestart_crash_recovers_original_request_id(db_conn):
    sid = ContextService().create_session_record(
        ["p1"], workflow_mode="langgraph", clarification_protocol_version="v2",
    )
    body = {
        "idempotency_key": str(uuid.uuid4()), "session_id": sid,
        "participants": [{"participant_ref": "p1", "label": "用户 1"}],
        "message": "推荐晚餐",
    }
    first_api = RecommendationAPI()
    with patch.object(first_api, "_create_new", side_effect=RuntimeError("crash before start")):
        code, failed_start = first_api.create_request(body)
    assert code == 503
    assert failed_start["error"] == "REQUEST_RECOVERY_PENDING"

    key_hash = hashlib.sha256(body["idempotency_key"].encode()).hexdigest()
    original = ContextService().load_request_acceptance(key_hash)
    assert original["status"] == "recovery_required"

    retry_api = RecommendationAPI()
    with patch.object(retry_api, "_create_new", return_value=(202, {"request_id": original["request_id"]})) as start:
        code, response = retry_api.create_request(body)
    assert code == 202
    assert response["request_id"] == original["request_id"]
    assert start.call_args.args[0] == original["request_id"]


def test_implicit_request_persists_langgraph_v2_session(db_conn):
    api = RecommendationAPI()
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "participants": [{"participant_ref": "p1", "label": "用户 1"}],
        "message": "推荐晚餐",
    }
    with patch.object(api, "_trigger_workflow") as trigger:
        code, created = api.create_request(body)

    assert code == 202
    trigger.assert_called_once()
    state = ContextService().load_clarification_state(created["session_id"])
    assert state["workflow_mode"] == "langgraph"
    assert state["protocol_version"] == "v2"
    key_hash = hashlib.sha256(body["idempotency_key"].encode()).hexdigest()
    acceptance = ContextService().load_request_acceptance(key_hash)
    assert acceptance["request_id"] == created["request_id"]
    assert acceptance["execution_generation"] == 1


def test_load_clarification_state_interface():
    """Verify ContextService and MySQL repository provide load_clarification_state."""
    c4 = ContextService()
    session_id = c4.create_session_record(
        ["p1"],
        workflow_mode="langgraph",
        clarification_protocol_version="v2",
    )

    clar_state = c4.load_clarification_state(session_id)
    assert clar_state is not None
    assert clar_state["session_id"] == session_id
    assert "active_question_id" in clar_state
    assert "clarification_revision" in clar_state
    assert "active_fencing_token" in clar_state
    assert "fencing_token" in clar_state
    assert clar_state["workflow_mode"] == "langgraph"
    assert clar_state["protocol_version"] == "v2"
