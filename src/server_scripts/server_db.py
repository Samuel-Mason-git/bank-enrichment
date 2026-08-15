import duckdb
import logging
import os

log = logging.getLogger(__name__)

_con: duckdb.DuckDBPyConnection | None = None

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "bank_enrichment_server.db")


def get_con() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _con


# (table, column, type) for columns added to a table that already exists in
# deployed databases. CREATE TABLE IF NOT EXISTS is a no-op once the table is
# there, so a column added to server_tables.sql never reaches the running
# server -- the deploy appears to succeed and the app then fails at runtime,
# against live webhooks. Every column added to an EXISTING table goes here.
#
# Deliberately empty: no such column has been added yet. The mechanism is here
# so the next one cannot be forgotten.
MIGRATIONS: list[tuple[str, str, str]] = []


def split_sql_statements(sql: str) -> list[str]:
    """Split a schema file into executable statements.

    Comments are stripped BEFORE splitting on the semicolon, because splitting
    naively meant a single semicolon inside a comment cut a CREATE TABLE in
    half and broke the entire schema load. Quotes are tracked so a '--' inside
    a string literal is left alone.

    (Twin of the same function in local_scripts/database_functions.py -- the
    server and local pipeline run in separate containers and share no code.)"""
    cleaned = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        in_string = False
        for i, char in enumerate(line):
            if char == "'":
                in_string = not in_string
            elif char == "-" and not in_string and line[i:i + 2] == "--":
                line = line[:i]
                break
        cleaned.append(line)
    return [s.strip() for s in "\n".join(cleaned).split(";") if s.strip()]


def _apply_migrations(con, migrations: list[tuple[str, str, str]]) -> list[str]:
    """Add any missing columns. Additive only -- nothing is dropped, renamed or
    rewritten, so it is safe on every startup and safe to re-run."""
    added = []
    for table, column, column_type in migrations:
        existing = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table],
        ).fetchall()}
        if not existing:
            continue  # table isn't there yet, so CREATE TABLE will include it
        if column not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            added.append(f"{table}.{column}")
    return added


def init_db() -> None:
    global _con
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _con = duckdb.connect(DB_PATH)
    sql_path = os.path.join(os.path.dirname(__file__), "..", "..", "sql", "server_tables.sql")
    with open(sql_path, "r") as f:
        sql = f.read()
    for statement in split_sql_statements(sql):
        _con.execute(statement)
    added = _apply_migrations(_con, MIGRATIONS)
    if added:
        log.info(f"Applied schema migrations: {', '.join(added)}")


def get_quick_categories(merchant_name: str | None) -> list[dict]:
    """Up to 3 subcategory suggestions for this merchant, else the top 5 overall."""
    con = get_con()
    if merchant_name:
        rows = con.execute(
            "SELECT id, category, subcategory FROM quick_categories WHERE merchant_name = ? ORDER BY rank LIMIT 3",
            [merchant_name]
        ).fetchall()
        if rows:
            log.info(f"get_quick_categories: {len(rows)} merchant-specific row(s) for {merchant_name!r}")
            return [{"id": r[0], "category": r[1], "subcategory": r[2]} for r in rows]
        log.info(f"get_quick_categories: no merchant-specific rows for {merchant_name!r}, falling back to general top-5")

    rows = con.execute(
        "SELECT id, category, subcategory FROM quick_categories WHERE merchant_name IS NULL ORDER BY rank LIMIT 5"
    ).fetchall()
    log.info(f"get_quick_categories: general fallback returned {len(rows)} row(s)")
    return [{"id": r[0], "category": r[1], "subcategory": r[2]} for r in rows]
