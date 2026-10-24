-- Today's tables, for Postgres: both sql/tables.sql (the local pipeline) and
-- sql/server_tables.sql (the server), in one schema, column for column.
--
-- This is stage 1 of docs/server-move.md: the same tables on a different
-- engine, nothing redesigned. tests/test_postgres_schema.py checks it still
-- matches the two DuckDB files, so a column added there must be added here too.
--
-- The two files share two table names, so the server's copies are renamed:
--   server taxonomy_proposals  -> server_taxonomy_proposals
--   server category_proposals  -> server_category_proposals
--
-- Dialect differences from the DuckDB files:
--   * default_role_for_parent is a SQL function here (DuckDB calls it a MACRO).
--   * Everything else -- types, defaults, CHECKs, keys -- is unchanged.

-- ── Local pipeline (sql/tables.sql) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS parent_categories (
    id       INTEGER PRIMARY KEY,
    name     VARCHAR NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS subcategories (
    id         INTEGER PRIMARY KEY,
    name       VARCHAR NOT NULL,
    parent_id  INTEGER NOT NULL REFERENCES parent_categories(id),
    created_at TIMESTAMP NOT NULL,
    UNIQUE (name, parent_id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    merchant_name VARCHAR(255),
    amount        DECIMAL(19,4) NOT NULL,
    frequency     VARCHAR(20) NOT NULL CHECK (frequency IN ('weekly', 'fortnightly', 'monthly', 'annual')),
    active        BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id VARCHAR PRIMARY KEY,
    amount DECIMAL(19,4) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    description TEXT,
    monzo_category VARCHAR(255),
    merchant_name VARCHAR(255),
    merchant_category VARCHAR(255),
    counterparty_name VARCHAR(255),
    is_load BOOLEAN,
    created_at TIMESTAMP,
    settled_at TIMESTAMP,
    raw_payload JSON,
    user_context TEXT,
    skipped BOOLEAN DEFAULT FALSE,
    received_at TIMESTAMP,
    enriched_at TIMESTAMP,
    processed_at TIMESTAMP,
    llm_category VARCHAR(255),
    llm_subcategory VARCHAR(255),
    llm_confidence DECIMAL(5,4),
    llm_model VARCHAR(255),
    classified_at TIMESTAMP,
    pending_category_proposal_id INTEGER
);

CREATE TABLE IF NOT EXISTS monthly_summaries (
    id           INTEGER PRIMARY KEY,
    send_date    VARCHAR(7) NOT NULL UNIQUE,
    total_spend  DECIMAL(19,4) NOT NULL,
    total_income DECIMAL(19,4) NOT NULL,
    net          DECIMAL(19,4) NOT NULL,
    total_invested DECIMAL(19,4),
    total_saved  DECIMAL(19,4),
    sent_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_summaries (
    id           INTEGER PRIMARY KEY,
    send_date    VARCHAR(10) NOT NULL UNIQUE,
    total_spend  DECIMAL(19,4) NOT NULL,
    total_income DECIMAL(19,4) NOT NULL,
    net          DECIMAL(19,4) NOT NULL,
    total_invested DECIMAL(19,4),
    total_saved  DECIMAL(19,4),
    sent_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS taxonomy_proposals (
    id             INTEGER PRIMARY KEY,
    parent_name    VARCHAR NOT NULL,
    source_sub     VARCHAR NOT NULL,
    action         VARCHAR NOT NULL DEFAULT 'create'
        CHECK (action IN ('create', 'move')),
    target_parent  VARCHAR NOT NULL DEFAULT '',
    proposed_sub   VARCHAR NOT NULL,
    rationale      VARCHAR NOT NULL,
    evidence_ids   JSON    NOT NULL,
    evidence_count INTEGER NOT NULL,
    status         VARCHAR NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'applied', 'expired')),
    proposed_at    TIMESTAMP NOT NULL,
    decided_at     TIMESTAMP,
    applied_at     TIMESTAMP,
    run_key        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS category_proposals (
    id                INTEGER PRIMARY KEY,
    options           JSON    NOT NULL,
    status            VARCHAR NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'selected', 'denied', 'applied', 'expired')),
    selected_option   INTEGER,
    regenerate_requested BOOLEAN DEFAULT FALSE,
    proposed_at       TIMESTAMP NOT NULL,
    decided_at        TIMESTAMP,
    applied_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS judge_reviews (
    txn_id       VARCHAR PRIMARY KEY,
    merchant_key VARCHAR,
    category     VARCHAR NOT NULL,
    subcategory  VARCHAR NOT NULL,
    outcome      VARCHAR NOT NULL CHECK (outcome IN ('approved', 'held', 'kept', 'changed')),
    reviewed_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS category_roles (
    parent_id      INTEGER NOT NULL REFERENCES parent_categories(id),
    subcategory_id INTEGER REFERENCES subcategories(id),
    role           VARCHAR NOT NULL CHECK (role IN ('income', 'spend', 'investment', 'saving', 'transfer', 'excluded')),
    UNIQUE (parent_id, subcategory_id)
);

CREATE OR REPLACE FUNCTION default_role_for_parent(parent_name VARCHAR) RETURNS VARCHAR
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE parent_name
        WHEN 'Income' THEN 'income'
        WHEN 'Investments' THEN 'investment'
        WHEN 'Transfers' THEN 'transfer'
    END
$$;

CREATE OR REPLACE VIEW transaction_roles AS
SELECT t.*, COALESCE(
    sub_role.role,
    parent_role.role,
    default_role_for_parent(pc.name),
    CASE WHEN pc.name IS NOT NULL THEN 'spend' END,
    CASE WHEN t.amount >= 0 THEN 'income' ELSE 'spend' END
) AS role
FROM transactions t
LEFT JOIN parent_categories pc ON pc.name = t.llm_category
LEFT JOIN subcategories sc ON sc.name = t.llm_subcategory AND sc.parent_id = pc.id
LEFT JOIN category_roles sub_role ON sub_role.subcategory_id = sc.id
LEFT JOIN category_roles parent_role ON parent_role.parent_id = pc.id AND parent_role.subcategory_id IS NULL;

-- ── Server (sql/server_tables.sql) ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS stats (
    id                  INTEGER     PRIMARY KEY DEFAULT 1,
    total_received      INTEGER     DEFAULT 0,
    total_amount_pence  BIGINT      DEFAULT 0,
    requests_sent       INTEGER     DEFAULT 0,
    total_enriched      INTEGER     DEFAULT 0,
    total_processed     INTEGER     DEFAULT 0
);

INSERT INTO stats (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS webhook_queue (
    id                  VARCHAR(255) NOT NULL PRIMARY KEY,
    payload             TEXT         NOT NULL,
    received_at         TIMESTAMP    NOT NULL,
    status              VARCHAR(20)  DEFAULT 'pending'
        CHECK (status IN ('pending', 'enriched', 'processed')),
    user_context        TEXT,
    enriched_at         TIMESTAMP,
    request_count       INTEGER      DEFAULT 0,
    skipped             BOOLEAN      DEFAULT FALSE,
    last_requested_at   TIMESTAMP,
    rule_category       VARCHAR(255),
    rule_subcategory    VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS rules (
    id            INTEGER      PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    match_field   VARCHAR(255) NOT NULL,
    match_type    VARCHAR(50)  NOT NULL DEFAULT 'contains',
    match_value   VARCHAR(255) NOT NULL,
    auto_context  VARCHAR(255) NOT NULL DEFAULT '',
    enabled       BOOLEAN      DEFAULT TRUE,
    match_field_2 VARCHAR(255),
    match_type_2  VARCHAR(50),
    match_value_2 VARCHAR(255),
    auto_skip     BOOLEAN      DEFAULT FALSE,
    category      VARCHAR(255),
    subcategory   VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS taxonomy (
    category      VARCHAR(255) NOT NULL,
    subcategory   VARCHAR(255) NOT NULL,
    PRIMARY KEY (category, subcategory)
);

CREATE TABLE IF NOT EXISTS server_taxonomy_proposals (
    local_id       INTEGER      PRIMARY KEY,
    parent_name    VARCHAR(255) NOT NULL,
    source_sub     VARCHAR(255) NOT NULL,
    action         VARCHAR(10)  NOT NULL DEFAULT 'create'
        CHECK (action IN ('create', 'move')),
    target_parent  VARCHAR(255) NOT NULL DEFAULT '',
    proposed_sub   VARCHAR(255) NOT NULL,
    rationale      TEXT         NOT NULL,
    evidence_count INTEGER      NOT NULL DEFAULT 0,
    examples       TEXT,
    status         VARCHAR(20)  NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'collected')),
    sent_at        TIMESTAMP    NOT NULL,
    decided_at     TIMESTAMP,
    follow_up_count INTEGER     DEFAULT 0,
    last_nudged_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS server_category_proposals (
    local_id          INTEGER      PRIMARY KEY,
    options           TEXT         NOT NULL,
    txn_count         INTEGER      NOT NULL DEFAULT 0,
    examples          TEXT,
    status            VARCHAR(20)  NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'selected', 'denied', 'collected')),
    selected_option   INTEGER,
    regenerate_requested BOOLEAN   DEFAULT FALSE,
    sent_at           TIMESTAMP    NOT NULL,
    decided_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rule_suggestions (
    id            INTEGER      PRIMARY KEY,
    match_field   VARCHAR(255) NOT NULL,
    match_value   VARCHAR(255) NOT NULL,
    category      VARCHAR(255) NOT NULL,
    subcategory   VARCHAR(255) NOT NULL,
    txn_count     INTEGER      NOT NULL DEFAULT 0,
    auto_context  VARCHAR(255) NOT NULL DEFAULT '',
    status        VARCHAR(20)  NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied')),
    proposed_at   TIMESTAMP    NOT NULL,
    decided_at    TIMESTAMP,
    UNIQUE (match_field, match_value)
);

CREATE TABLE IF NOT EXISTS quick_categories (
    id            INTEGER      PRIMARY KEY,
    category      VARCHAR(255) NOT NULL,
    subcategory   VARCHAR(255) NOT NULL,
    merchant_name VARCHAR(255),
    rank          INTEGER      NOT NULL DEFAULT 0
);
