CREATE TABLE IF NOT EXISTS webhook_queue (
    id VARCHAR(255) NOT NULL PRIMARY KEY,
    payload TEXT NOT NULL,
    received_at TIMESTAMP NOT NULL,
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    status VARCHAR(20) DEFAULT 'pending' 
        CHECK (status IN ('pending', 'enriched', 'processed'))
);

CREATE TABLE IF NOT EXISTS enrichments (
    id SERIAL PRIMARY KEY,
    webhook_id VARCHAR(255) NOT NULL,
    enrichment_status VARCHAR(20) NOT NULL CHECK (enrichment_status IN ('Requested', 'Completed')),
    enrichment_data JSONB NOT NULL,
    enriched_at TIMESTAMP NOT NULL,
    FOREIGN KEY (webhook_id) REFERENCES webhook_queue(id) ON DELETE CASCADE
);