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
    sql_path = os.path.join(os.path.dirname(__file__), "..", "..", "sql", "server_tables.sql")
    with open(sql_path, "r") as f:
        sql = f.read()
    for statement in sql.split(";"):
        statement = statement.strip()
        if statement:
            _con.execute(statement)

    _con.execute(
        "ALTER TABLE webhook_queue ADD COLUMN IF NOT EXISTS request_count INTEGER DEFAULT 0"
    )
    _con.execute(
        "ALTER TABLE webhook_queue ADD COLUMN IF NOT EXISTS skipped BOOLEAN DEFAULT FALSE"
    )
    _con.execute(
        "ALTER TABLE webhook_queue ADD COLUMN IF NOT EXISTS last_requested_at TIMESTAMP"
    )
    _con.execute(
        """UPDATE webhook_queue
           SET last_requested_at = received_at
           WHERE request_count > 0 AND last_requested_at IS NULL"""
    )
    _con.execute("ALTER TABLE webhook_queue DROP COLUMN IF EXISTS user_category")
    _con.execute("ALTER TABLE webhook_queue DROP COLUMN IF EXISTS user_tags")
    _con.execute("ALTER TABLE rules ADD COLUMN IF NOT EXISTS match_field_2 VARCHAR(255)")
    _con.execute("ALTER TABLE rules ADD COLUMN IF NOT EXISTS match_type_2 VARCHAR(50)")
    _con.execute("ALTER TABLE rules ADD COLUMN IF NOT EXISTS match_value_2 VARCHAR(255)")