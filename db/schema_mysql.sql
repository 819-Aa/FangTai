-- V2 MySQL Schema
-- 与 V1 隔离：使用独立数据库 food_agent_v2

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
    match_method VARCHAR(32),
    confidence VARCHAR(16),
    coverage_ratio FLOAT DEFAULT 0,
    nutrient_values JSON,
    FOREIGN KEY (recipe_id) REFERENCES recipes(recipe_id)
);

CREATE TABLE IF NOT EXISTS time_profiles (
    recipe_id INT PRIMARY KEY,
    total_steps INT DEFAULT 0,
    total_active_seconds INT DEFAULT 0,
    total_equipment_seconds INT DEFAULT 0,
    total_passive_seconds INT DEFAULT 0,
    step_tasks JSON,
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_request_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_request (request_id)
);
