-- Migration: Add clarification questions lifecycle ledger
-- Design: docs/superpowers/specs/2026-09-27-agent-clarification-lifecycle-design.md
-- Closure Plan: docs/superpowers/plans/2026-09-28-clarification-v2-closure.md

-- 1. 为 sessions 表添加活跃问题、版本、协议与活跃 fencing 标记
ALTER TABLE sessions
    ADD COLUMN active_clarification_question_id VARCHAR(64) NULL,
    ADD COLUMN clarification_revision INT NOT NULL DEFAULT 0,
    ADD COLUMN workflow_mode VARCHAR(32) NULL DEFAULT NULL,
    ADD COLUMN clarification_protocol_version VARCHAR(16) NULL DEFAULT NULL,
    ADD COLUMN active_fencing_token BIGINT NULL DEFAULT NULL;

-- 2. 创建 clarification_questions 澄清账本表
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
);

-- 3. 创建 request_acceptances 请求接受身份表
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
);
