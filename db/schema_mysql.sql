-- V2 MySQL Schema
-- 与 V1 隔离：使用独立数据库 food_agent_v2

-- T09 一次性固定数据初始化元数据。data_builds 只有 ready 才可被在线读取；
-- fixed_artifact_records 在同一事务中保存 BuildManifest 已验证的无损产物。
CREATE TABLE IF NOT EXISTS data_builds (
    build_id CHAR(36) PRIMARY KEY,
    source_manifest_hash CHAR(64) NOT NULL,
    builder_version CHAR(40) NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL,
    quality_report_sha256 CHAR(64) NOT NULL,
    schema_versions JSON NOT NULL,
    artifact_counts JSON,
    status ENUM('initializing', 'ready') NOT NULL,
    initialized_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fixed_artifact_records (
    build_id CHAR(36) NOT NULL,
    artifact_name VARCHAR(96) NOT NULL,
    record_index INT NOT NULL,
    payload JSON NOT NULL,
    PRIMARY KEY (build_id, artifact_name, record_index),
    CONSTRAINT fk_fixed_artifact_build
        FOREIGN KEY (build_id) REFERENCES data_builds(build_id) ON DELETE CASCADE,
    INDEX idx_fixed_artifact_name (artifact_name)
);

CREATE TABLE IF NOT EXISTS recipes (
    recipe_id INT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    ingredients_raw TEXT,
    steps_raw TEXT,
    labels_raw TEXT,
    record_type VARCHAR(32) DEFAULT 'dish',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingredients (
    ingredient_id INT PRIMARY KEY,
    name_canonical VARCHAR(255) NOT NULL,
    category VARCHAR(64),
    family_id INT,
    is_edible BOOLEAN DEFAULT TRUE,
    is_basic_pantry BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingredient_aliases (
    alias_id INT AUTO_INCREMENT PRIMARY KEY,
    ingredient_id INT NOT NULL,
    alias_name VARCHAR(255) NOT NULL,
    match_type VARCHAR(32) DEFAULT 'exact',
    FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id)
);

CREATE TABLE IF NOT EXISTS health_relations (
    relation_id INT AUTO_INCREMENT PRIMARY KEY,
    constraint_code VARCHAR(128) NOT NULL,
    ingredient_id INT NOT NULL,
    review_status VARCHAR(32) DEFAULT 'pending',
    hard_filter BOOLEAN DEFAULT TRUE,
    evidence_ref VARCHAR(512),
    FOREIGN KEY (ingredient_id) REFERENCES ingredients(ingredient_id),
    UNIQUE KEY uk_constraint_ingredient (constraint_code, ingredient_id)
);

CREATE TABLE IF NOT EXISTS nutrition_profiles (
    recipe_id INT PRIMARY KEY,
    available BOOLEAN NOT NULL,
    raw_edible_input_weight_g DECIMAL(12, 2),
    raw_nutrition_total JSON,
    raw_nutrition_per_100g JSON,
    reason VARCHAR(64),
    FOREIGN KEY (recipe_id) REFERENCES recipes(recipe_id)
);

CREATE TABLE IF NOT EXISTS time_profiles (
    recipe_id INT PRIMARY KEY,
    active_seconds INT NOT NULL,
    estimated_elapsed_seconds INT NOT NULL,
    step_tasks JSON NOT NULL,
    FOREIGN KEY (recipe_id) REFERENCES recipes(recipe_id)
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id INT PRIMARY KEY,
    gender VARCHAR(8),
    age INT,
    activity_level VARCHAR(32),
    special_group VARCHAR(64),
    height_cm FLOAT,
    weight_kg FLOAT,
    bmi FLOAT,
    dietary_preferences JSON,
    allergies JSON,
    health_goals JSON,
    diseases JSON,
    taboo_ingredients JSON,
    health_metrics JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id VARCHAR(64) PRIMARY KEY,
    participant_refs JSON,
    current_menu_plan_id VARCHAR(64),
    request_count INT DEFAULT 0,
    fencing_token BIGINT NULL,
    active_clarification_question_id VARCHAR(64) NULL,
    clarification_revision INT NOT NULL DEFAULT 0,
    workflow_mode VARCHAR(32) NULL DEFAULT NULL,
    clarification_protocol_version VARCHAR(16) NULL DEFAULT NULL,
    active_fencing_token BIGINT NULL DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_request_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

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

CREATE TABLE IF NOT EXISTS conversation_events (
    event_id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(64),
    event_type VARCHAR(32) NOT NULL,
    event_summary TEXT,
    event_detail_ref TEXT,
    participant_refs JSON,
    token_count_estimate INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS menu_versions (
    version_id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    plan_id VARCHAR(64) NOT NULL,
    menu_hash VARCHAR(64),
    recipe_ids JSON,
    committed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS recommendation_logs (
    log_id INT AUTO_INCREMENT PRIMARY KEY,
    request_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64),
    status VARCHAR(32),
    final_plan_id VARCHAR(64),
    health_evidence JSON,
    commit_hash VARCHAR(64) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_request (request_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    event_id VARCHAR(64) PRIMARY KEY,
    request_id VARCHAR(64) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    payload JSON,
    seq INT NOT NULL DEFAULT 0,
    status VARCHAR(16) DEFAULT 'pending',
    claim_token VARCHAR(64) NULL,
    claimed_at TIMESTAMP NULL,
    dispatched_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
