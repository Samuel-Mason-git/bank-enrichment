"""sql/postgres/schema.sql must stay the same tables as the two DuckDB schema
files -- stage 1 of docs/server-move.md moves today's tables to Postgres
unchanged, and the copy script relies on every table and column lining up.

These need a Postgres (the `pg` fixture), so they run in CI and are skipped on
a machine without one."""
from decimal import Decimal

import duckdb

from tests.conftest import _SERVER_SQL_PATH, _SQL_PATH
from database_functions import split_sql_statements

# The two DuckDB files share these table names, so the Postgres schema renames
# the server's copies.
SERVER_RENAMES = {
    "taxonomy_proposals": "server_taxonomy_proposals",
    "category_proposals": "server_category_proposals",
}


def _duckdb_from(path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for statement in split_sql_statements(path.read_text()):
        con.execute(statement)
    return con


def _duckdb_type(data_type: str) -> str:
    t = data_type.upper()
    if t.startswith("DECIMAL"):
        return t.lower()  # e.g. decimal(19,4)
    return {
        "VARCHAR": "text", "INTEGER": "integer", "BIGINT": "bigint",
        "TIMESTAMP": "timestamp", "BOOLEAN": "boolean", "JSON": "json",
    }[t]


def _postgres_type(data_type: str, precision, scale) -> str:
    if data_type == "numeric":
        return f"decimal({precision},{scale})"
    return {
        "character varying": "text", "text": "text", "integer": "integer",
        "bigint": "bigint", "timestamp without time zone": "timestamp",
        "boolean": "boolean", "json": "json",
    }[data_type]


def _duckdb_tables(con, renames=None) -> dict[str, dict[str, tuple[str, str]]]:
    """{table: {column: (type, is_nullable)}} for base tables (not views)."""
    renames = renames or {}
    rows = con.execute(
        """SELECT c.table_name, c.column_name, c.data_type, c.is_nullable
           FROM information_schema.columns c
           JOIN information_schema.tables t USING (table_schema, table_name)
           WHERE t.table_type = 'BASE TABLE'"""
    ).fetchall()
    tables: dict[str, dict] = {}
    for table, column, data_type, nullable in rows:
        tables.setdefault(renames.get(table, table), {})[column] = (_duckdb_type(data_type), nullable)
    return tables


def _postgres_tables(pg) -> dict[str, dict[str, tuple[str, str]]]:
    rows = pg.execute(
        """SELECT c.table_name, c.column_name, c.data_type,
                  c.numeric_precision, c.numeric_scale, c.is_nullable
           FROM information_schema.columns c
           JOIN information_schema.tables t USING (table_schema, table_name)
           WHERE t.table_type = 'BASE TABLE' AND c.table_schema = current_schema()"""
    ).fetchall()
    tables: dict[str, dict] = {}
    for table, column, data_type, precision, scale, nullable in rows:
        tables.setdefault(table, {})[column] = (_postgres_type(data_type, precision, scale), nullable)
    return tables


def test_postgres_has_exactly_the_duckdb_tables_and_columns(pg):
    expected = _duckdb_tables(_duckdb_from(_SQL_PATH))
    expected.update(_duckdb_tables(_duckdb_from(_SERVER_SQL_PATH), SERVER_RENAMES))
    actual = _postgres_tables(pg)

    assert sorted(actual) == sorted(expected)
    for table in expected:
        assert actual[table] == expected[table], f"{table} differs"


def test_the_stats_row_is_seeded(pg):
    assert pg.execute("SELECT id, total_received FROM stats").fetchall() == [(1, 0)]


# One row per way a role can be decided, in transaction_roles' COALESCE order.
_ROLE_CASES = [
    # (id, amount, category, subcategory)
    ("sub_override", -5, "Food", "Groceries"),      # subcategory role set: saving
    ("parent_override", -5, "Fun", "Gigs"),         # parent role set: excluded
    ("default_income", 900, "Income", "Salary"),    # built-in default by name
    ("default_invest", -50, "Investments", "ISA"),
    ("default_transfer", -10, "Transfers", "Pot"),
    ("plain_spend", -3, "Food", "Takeaway"),        # classified, no role: spend
    ("unclassified_in", 7, None, None),             # unclassified: by sign
    ("unclassified_out", -7, None, None),
]


def _seed_roles(execute):
    for i, name in enumerate(["Food", "Fun", "Income", "Investments", "Transfers"], start=1):
        execute("INSERT INTO parent_categories VALUES (?, ?, '2026-01-01')", [i, name])
    subs = [("Groceries", 1), ("Takeaway", 1), ("Gigs", 2), ("Salary", 3), ("ISA", 4), ("Pot", 5)]
    for i, (name, parent) in enumerate(subs, start=1):
        execute("INSERT INTO subcategories VALUES (?, ?, ?, '2026-01-01')", [i, name, parent])
    execute("INSERT INTO category_roles VALUES (1, 1, 'saving')", [])
    execute("INSERT INTO category_roles VALUES (2, NULL, 'excluded')", [])
    for txn_id, amount, category, subcategory in _ROLE_CASES:
        execute(
            "INSERT INTO transactions (id, amount, currency, llm_category, llm_subcategory) "
            "VALUES (?, ?, 'GBP', ?, ?)",
            [txn_id, Decimal(amount), category, subcategory],
        )


def test_transaction_roles_gives_the_same_answers_on_both_engines(db, pg):
    _seed_roles(lambda sql, params: db.execute(sql, params))
    # psycopg uses %s placeholders where DuckDB uses ?
    _seed_roles(lambda sql, params: pg.execute(sql.replace("?", "%s"), params))

    query = "SELECT id, role FROM transaction_roles ORDER BY id"
    duck = db.execute(query).fetchall()
    assert pg.execute(query).fetchall() == duck
    assert dict(duck) == {
        "sub_override": "saving", "parent_override": "excluded",
        "default_income": "income", "default_invest": "investment",
        "default_transfer": "transfer", "plain_spend": "spend",
        "unclassified_in": "income", "unclassified_out": "spend",
    }
