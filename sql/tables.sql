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
    -- Monzo data (flattened for querying)
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
    -- Full raw payload preserved
    raw_payload JSON,
    -- Human context
    user_context TEXT,
    skipped BOOLEAN DEFAULT FALSE,
    -- Timestamps
    received_at TIMESTAMP,
    enriched_at TIMESTAMP,
    processed_at TIMESTAMP,
    -- LLM classification
    llm_category VARCHAR(255),
    llm_subcategory VARCHAR(255),
    llm_confidence DECIMAL(5,4),
    llm_model VARCHAR(255),
    classified_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monthly_summaries (
    id           INTEGER PRIMARY KEY,
    send_date    VARCHAR(7) NOT NULL UNIQUE,  -- e.g. '2026-05'
    total_spend  DECIMAL(19,4) NOT NULL,
    total_income DECIMAL(19,4) NOT NULL,
    net          DECIMAL(19,4) NOT NULL,
    sent_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_summaries (
    id           INTEGER PRIMARY KEY,
    send_date    VARCHAR(10) NOT NULL UNIQUE,  -- e.g. '2026-W26'
    total_spend  DECIMAL(19,4) NOT NULL,
    total_income DECIMAL(19,4) NOT NULL,
    net          DECIMAL(19,4) NOT NULL,
    sent_at      TIMESTAMP NOT NULL
);

-- User-set Income/Spend/Investment/Transfer/Excluded role per category.
-- subcategory_id NULL = applies to the whole parent. Empty until edited in Settings.
CREATE TABLE IF NOT EXISTS category_roles (
    parent_id      INTEGER NOT NULL REFERENCES parent_categories(id),
    subcategory_id INTEGER REFERENCES subcategories(id),
    role           VARCHAR NOT NULL CHECK (role IN ('income', 'spend', 'investment', 'transfer', 'excluded')),
    UNIQUE (parent_id, subcategory_id)
);

-- Built-in default role by category name, used when there's no override.
CREATE OR REPLACE MACRO default_role_for_parent(parent_name) AS
    CASE parent_name
        WHEN 'Income' THEN 'income'
        WHEN 'Investments' THEN 'investment'
        WHEN 'Transfers' THEN 'transfer'
    END;

-- Each transaction's role: override, else built-in default, else spend, else
-- (only if totally unclassified) amount sign.
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