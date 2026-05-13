import duckdb
import os

_con: duckdb.DuckDBPyConnection | None = None

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "bank_enrichment_server.db")


def get_con() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _con


def init_db() -> None:
    global _con
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _con = duckdb.connect(DB_PATH)
    _con.execute("""
        CREATE TABLE IF NOT EXISTS webhook_queue (
            id          VARCHAR     NOT NULL PRIMARY KEY,
            payload     TEXT        NOT NULL,
            received_at TIMESTAMP   NOT NULL,
            processed   BOOLEAN     NOT NULL DEFAULT FALSE,
            status      VARCHAR(20) DEFAULT 'pending'
                CHECK (status IN ('pending', 'enriched', 'processed'))
        )
    """)
    _con.execute("CREATE SEQUENCE IF NOT EXISTS enrichments_id_seq START 1")
    _con.execute("""
        CREATE TABLE IF NOT EXISTS enrichments (
            id                INTEGER    PRIMARY KEY DEFAULT nextval('enrichments_id_seq'),
            webhook_id        VARCHAR    NOT NULL,
            enrichment_status VARCHAR(20) NOT NULL
                CHECK (enrichment_status IN ('Requested', 'Completed')),
            enrichment_data   JSON       NOT NULL,
            enriched_at       TIMESTAMP  NOT NULL,
            FOREIGN KEY (webhook_id) REFERENCES webhook_queue(id) ON DELETE CASCADE
        )
    """)
