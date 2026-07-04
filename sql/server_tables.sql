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
    last_requested_at   TIMESTAMP
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
    auto_skip     BOOLEAN      DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS quick_categories (
    id            INTEGER      PRIMARY KEY,
    category      VARCHAR(255) NOT NULL,
    subcategory   VARCHAR(255) NOT NULL,
    merchant_name VARCHAR(255),
    rank          INTEGER      NOT NULL DEFAULT 0
);
