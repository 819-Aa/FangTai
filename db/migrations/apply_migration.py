"""Apply database migrations idempotently."""

import json

import pymysql

from food_agent_v2.core.config import load_config


def apply_migrations(cfg=None):
    if cfg is None:
        cfg = load_config()

    conn = pymysql.connect(
        host=cfg.mysql.host,
        port=cfg.mysql.port,
        user=cfg.mysql.user,
        password=cfg.mysql.password,
        database=cfg.mysql.database,
        charset="utf8mb4",
    )
    cur = conn.cursor()

    try:
        # 1. 逐列检查 sessions 表中的扩展字段，做纯加法迁移
        cur.execute(
            """
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'sessions'
            """,
            (cfg.mysql.database,),
        )
        existing_cols = {r[0] for r in cur.fetchall()}

        columns_to_add = [
            ("active_clarification_question_id", "VARCHAR(64) NULL"),
            ("clarification_revision", "INT NOT NULL DEFAULT 0"),
            ("workflow_mode", "VARCHAR(32) NULL DEFAULT NULL"),
            ("clarification_protocol_version", "VARCHAR(16) NULL DEFAULT NULL"),
            ("active_fencing_token", "BIGINT NULL DEFAULT NULL"),
        ]

        for col_name, col_def in columns_to_add:
            if col_name not in existing_cols:
                print(f"Adding column {col_name} to sessions table...")
                cur.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_def}")
                conn.commit()

        if "active_fencing_token" in existing_cols:
            cur.execute(
                "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='sessions' "
                "AND COLUMN_NAME='active_fencing_token'",
                (cfg.mysql.database,),
            )
            if cur.fetchone()[0].lower() != "bigint":
                cur.execute(
                    "ALTER TABLE sessions MODIFY COLUMN active_fencing_token BIGINT NULL DEFAULT NULL"
                )
                conn.commit()

        # 2. 确认 clarification_questions 表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clarification_questions (
                question_id VARCHAR(64) PRIMARY KEY,
                session_id VARCHAR(64) NOT NULL,
                producer_request_id VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                public_payload JSON NOT NULL,
                private_snapshot JSON NOT NULL,
                expires_at TIMESTAMP NULL,
                accepted_request_id VARCHAR(64) NULL,
                accepted_option_id INT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_producer_request (producer_request_id),
                UNIQUE KEY uk_accepted_request (accepted_request_id),
                INDEX idx_session_status_expires (session_id, status, expires_at),
                CONSTRAINT fk_clarification_session
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
            """
        )
        conn.commit()

        # 3. 确认 request_acceptances 表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS request_acceptances (
                idempotency_key_hash CHAR(64) PRIMARY KEY,
                payload_hash CHAR(64) NOT NULL,
                request_id VARCHAR(64) NOT NULL UNIQUE,
                session_id VARCHAR(64) NOT NULL,
                status VARCHAR(24) NOT NULL DEFAULT 'accepted',
                execution_owner VARCHAR(64) NULL,
                execution_generation BIGINT NOT NULL DEFAULT 0,
                execution_lease_until TIMESTAMP NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='request_acceptances'",
            (cfg.mysql.database,),
        )
        acceptance_cols = {row[0] for row in cur.fetchall()}
        for col, definition in (
            ("execution_owner", "VARCHAR(64) NULL"),
            ("execution_generation", "BIGINT NOT NULL DEFAULT 0"),
            ("execution_lease_until", "TIMESTAMP NULL"),
        ):
            if col not in acceptance_cols:
                cur.execute(f"ALTER TABLE request_acceptances ADD COLUMN {col} {definition}")
                conn.commit()

        cur.execute(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='request_acceptances' AND COLUMN_NAME='status'",
            (cfg.mysql.database,),
        )
        status_width = cur.fetchone()[0]
        if status_width is not None and int(status_width) < 24:
            cur.execute("ALTER TABLE request_acceptances MODIFY COLUMN status "
                        "VARCHAR(24) NOT NULL DEFAULT 'accepted'")
            conn.commit()

        # 4. 数据净化与字段隔离迁移：
        # 若已有问题的 public_payload 中残留 modifications，将其移入 private_snapshot.option_modifications 并净化公开负载
        cur.execute("SELECT question_id, public_payload, private_snapshot FROM clarification_questions")
        rows = cur.fetchall()
        for qid, pub_raw, priv_raw in rows:
            pub = json.loads(pub_raw) if isinstance(pub_raw, str) else dict(pub_raw or {})
            priv = json.loads(priv_raw) if isinstance(priv_raw, str) else dict(priv_raw or {})
            dirty = False
            extracted_mods = priv.get("option_modifications") or {}
            for opt in pub.get("options", []):
                if isinstance(opt, dict) and "modifications" in opt:
                    opt_id = opt.get("option_id")
                    if opt_id is not None:
                        extracted_mods[int(opt_id)] = opt.pop("modifications")
                    else:
                        opt.pop("modifications")
                    dirty = True
            if dirty:
                priv["option_modifications"] = extracted_mods
                cur.execute(
                    """
                    UPDATE clarification_questions
                    SET public_payload = %s, private_snapshot = %s
                    WHERE question_id = %s
                    """,
                    (json.dumps(pub, ensure_ascii=False), json.dumps(priv, ensure_ascii=False), qid),
                )
                conn.commit()
                print(f"Sanitized public_payload and updated private_snapshot for question: {qid}")

    finally:
        cur.close()
        conn.close()


def main():
    apply_migrations()


if __name__ == "__main__":
    main()
