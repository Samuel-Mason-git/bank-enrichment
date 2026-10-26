"""Splitting the old schema files into statements.

Only the copy script needs this now: it builds the old DuckDB shapes from
sql/tables.sql and sql/server_tables.sql to read them. The new schema
(sql/postgres/schema.sql) goes to Postgres whole, and adds its own columns with
ADD COLUMN IF NOT EXISTS (tests/test_postgres_schema.py).

The failure it guards against: a semicolon inside a comment once cut a CREATE
TABLE in half, so the whole schema load failed on a fragment.
"""
from pathlib import Path

import duckdb

from server_db import split_sql_statements as split


class TestSplittingSchemaFiles:
    def test_splits_on_statement_boundaries(self):
        assert split("CREATE TABLE a (x INT); CREATE TABLE b (y INT)") == [
            "CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"]

    def test_a_semicolon_in_a_comment_does_not_split_a_statement(self):
        """The exact failure: a comment reading "...for approval; the pipeline
        applies it" cut the following CREATE TABLE in half."""
        sql = """-- Sent for approval; the pipeline applies whatever comes back.
CREATE TABLE proposals (id INTEGER PRIMARY KEY, name VARCHAR)"""
        statements = split(sql)
        assert len(statements) == 1
        assert statements[0].startswith("CREATE TABLE proposals")

    def test_a_trailing_comment_with_a_semicolon_is_stripped(self):
        sql = "CREATE TABLE a (\n  run_key VARCHAR  -- e.g. '2026-08'; one per month\n)"
        statements = split(sql)
        assert len(statements) == 1
        assert "one per month" not in statements[0]

    def test_a_double_dash_inside_a_string_literal_is_preserved(self):
        sql = "INSERT INTO a VALUES ('before -- after')"
        assert split(sql) == ["INSERT INTO a VALUES ('before -- after')"]

    def test_blank_lines_and_trailing_semicolons_produce_no_empty_statements(self):
        assert split("CREATE TABLE a (x INT);\n\n\n;  \n") == ["CREATE TABLE a (x INT)"]

    def test_the_old_schema_files_still_parse(self):
        root = Path(__file__).parent.parent / "sql"
        for name in ("tables.sql", "server_tables.sql"):
            con = duckdb.connect(":memory:")
            for statement in split((root / name).read_text()):
                con.execute(statement)
            assert con.execute("SHOW TABLES").fetchall(), name
            con.close()
