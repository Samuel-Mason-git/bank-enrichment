CREATE TABLE IF NOT EXISTS webhook_queue (
    id VARCHAR(255) NOT NULL PRIMARY KEY,
    payload TEXT NOT NULL,
    received_at TIMESTAMP NOT NULL,
    user_context TEXT,
    user_category TEXT,
    user_tags TEXT,
    enriched_at TIMESTAMP,
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    status VARCHAR(20) DEFAULT 'pending' 
        CHECK (status IN ('pending', 'enriched', 'processed'))
);