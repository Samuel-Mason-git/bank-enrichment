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

-- Taxonomy proposals pushed up from the local pipeline, awaiting an Approve or
-- Deny tap in Telegram. The server never applies anything itself -- it only
-- records the decision, which the local pipeline collects on its next run.
-- local_id is the id in the LOCAL database, which is what decisions key on.
CREATE TABLE IF NOT EXISTS taxonomy_proposals (
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

-- Real-time category-creation proposals, mirroring the local table by
-- local_id. The server only records the Telegram decision -- creating the
-- category and classifying the waiting transactions happens locally.
-- options is the same JSON-encoded list of candidate placements as the local
-- table's `options` column (up to a few, so the user can pick one instead of
-- the classifier having to commit to a single guess).
CREATE TABLE IF NOT EXISTS category_proposals (
    local_id          INTEGER      PRIMARY KEY,
    options           TEXT         NOT NULL,
    txn_count         INTEGER      NOT NULL DEFAULT 0,
    examples          TEXT,
    status            VARCHAR(20)  NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'selected', 'denied', 'collected')),
    selected_option   INTEGER,
    sent_at           TIMESTAMP    NOT NULL,
    decided_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quick_categories (
    id            INTEGER      PRIMARY KEY,
    category      VARCHAR(255) NOT NULL,
    subcategory   VARCHAR(255) NOT NULL,
    merchant_name VARCHAR(255),
    rank          INTEGER      NOT NULL DEFAULT 0
);
