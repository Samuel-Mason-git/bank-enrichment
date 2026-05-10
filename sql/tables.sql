/*
 * Monzo Financial Enrichment Schema
 * ==================================
 *
 * OVERVIEW
 * --------
 * This schema captures raw Monzo transaction data, enriches it with human
 * context, and stores LLM-generated classifications separately. The goal is
 * to build a rich, structured personal financial dataset that can be
 * reanalysed in the future as categories evolve and models improve.
 *
 * TABLE FLOW
 * ----------
 *
 * 1. RAW LAYER
 *    monzo_accounts → monzo_payments
 *    Transactions arrive via Monzo webhook and are stored immediately in their
 *    raw form. This layer is immutable — nothing is ever updated or deleted.
 *    Merchant data is stored as JSONB to preserve the full Monzo payload
 *    without flattening it into columns that may change over time.
 *
 * 2. ENRICHMENT QUEUE
 *    pending_enrichments
 *    Every new transaction creates a corresponding pending enrichment record.
 *    This acts as the processing queue — when the user's PC comes online it
 *    polls this table for anything with status 'pending' and surfaces those
 *    transactions for the user to enrich. Status moves from 'pending' →
 *    'completed' or 'failed'.
 *
 * 3. HUMAN ENRICHMENT LAYER
 *    enriched_payments → USER_TAG_JUNCTIONS → user_tags
 *                      → USER_CATEGORY_JUNCTIONS → categories → parent_categories
 *    Once the user enriches a transaction they provide:
 *      - A free text context sentence (what this actually was, why, who with)
 *      - One or more user-managed categories (hierarchical, parent/subcategory)
 *      - One or more user-managed tags (freeform labels they build over time)
 *    Users own this layer entirely. They can add new categories and tags at
 *    any time. This is the most valuable part of the dataset — the human
 *    context that raw transaction data cannot capture.
 *
 * 4. LLM CLASSIFICATION LAYER
 *    llm_classifications
 *    Separate from user input, this table stores AI-generated classifications
 *    produced by running enriched transactions through an LLM. The LLM reads
 *    the user context, user categories, and raw transaction data and produces
 *    its own grouping label and confidence score. This layer is derived and
 *    re-runnable — the raw and human layers are never modified. Every
 *    classification run is versioned by model name and timestamp, meaning
 *    classifications from different models at different points in time can be
 *    compared. This is what enables queries like "how much did I spend on
 *    coffee in 2026" even if the category taxonomy has changed completely
 *    by the time the query is run.
 *
 * KEY DESIGN DECISIONS
 * --------------------
 * - Raw and enriched data are kept in separate tables so the source of truth
 *   is never overwritten
 * - User categories and LLM categories are kept separate so neither
 *   overwrites the other and both can be queried independently
 * - Tags and categories use junction tables so they can be queried efficiently
 *   and renamed or reorganised without data loss
 * - Money is stored as DECIMAL(19,4) not FLOAT to avoid floating point
 *   precision errors
 * - Merchant data is stored as JSONB to future-proof against Monzo API changes
 */

CREATE TABLE IF NOT EXISTS monzo_payments (
    account_id VARCHAR(255) NOT NULL FOREIGN KEY REFERENCES monzo_accounts(id),
    amount DECIMAL(19, 4) NOT NULL,
    created TIMESTAMP NOT NULL,
    currency VARCHAR(3) NOT NULL,
    description TEXT,
    id VARCHAR(255) NOT NULL PRIMARY KEY,
    category VARCHAR(255),
    is_load BOOLEAN NOT NULL,
    settled TIMESTAMP,
    merchant JSONB
);

CREATE TABLE IF NOT EXISTS pending_enrichments (
    id VARCHAR(255) NOT NULL PRIMARY KEY,
    account_id VARCHAR(255) NOT NULL FOREIGN KEY REFERENCES monzo_accounts(id),
    payment_id VARCHAR(255) NOT NULL FOREIGN KEY REFERENCES monzo_payments(id),
    created_at TIMESTAMP NOT NULL,
    enrichment_status VARCHAR(255) NOT NULL DEFAULT 'pending' CHECK (enrichment_status IN ('pending', 'completed', 'failed'))
);

CREATE TABLE IF NOT EXISTS enriched_payments (
    id VARCHAR(255) NOT NULL PRIMARY KEY,
    payment_id VARCHAR(255) NOT NULL FOREIGN KEY REFERENCES monzo_payments(id),
    collected_metadata_at TIMESTAMP NOT NULL,
    user_context VARCHAR(255),
    user_tags JSONB
);

CREATE TABLE IF NOT EXISTS user_tags (
    user_tag_id INTEGER NOT NULL PRIMARY KEY,
    user_tag VARCHAR(255) NOT NULL
)

CREATE TABLE IF NOT EXISTS USER_TAG_JUNCTIONS (
    user_tag_id INTEGER NOT NULL FOREIGN KEY REFERENCES user_tags(user_tag_id),
    enriched_payment_id VARCHAR(255) NOT NULL FOREIGN KEY REFERENCES enriched_payments(id),
    PRIMARY KEY (user_tag_id, enriched_payment_id)
);

CREATE TABLE IF NOT EXISTS parent_categories (
    parent_category_id INTEGER NOT NULL PRIMARY KEY,
    parent_category VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    category_id INTEGER NOT NULL PRIMARY KEY,
    category VARCHAR(255) NOT NULL,
    parent_category_id INTEGER foreign key references parent_categories(parent_category_id)
);

CREATE TABLE IF NOT EXISTS USER_CATEGORY_JUNCTIONS (
    category_id INTEGER NOT NULL FOREIGN KEY REFERENCES categories(category_id),
    enriched_payment_id VARCHAR(255) NOT NULL FOREIGN KEY REFERENCES enriched_payments(id),
    PRIMARY KEY (category_id, enriched_payment_id)
);

CREATE TABLE IF NOT EXISTS llm_classifications (
    id VARCHAR(255) NOT NULL PRIMARY KEY,
    enriched_payment_id VARCHAR(255) NOT NULL FOREIGN KEY REFERENCES enriched_payments(id),
    model_name VARCHAR(255) NOT NULL,
    classification VARCHAR(255) NOT NULL,
    confidence_score DECIMAL(5, 4) NOT NULL,
    classified_at TIMESTAMP NOT NULL
);