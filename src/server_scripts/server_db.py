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