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
