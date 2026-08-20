"""Schema loading and additive migrations, for both databases.

Two silent-failure modes motivated these. A semicolon inside a comment used to
cut a CREATE TABLE in half, so the whole schema load failed on a fragment. And
CREATE TABLE IF NOT EXISTS is a no-op once a table exists, so a column added to
the .sql file never reached a database that already had that table -- on the
server that meant a deploy appearing to succeed and the app then failing at
runtime against live webhooks.
"""
import duckdb
import pytest

import database_functions
import server_db


@pytest.fixture(params=["local", "server"])
def split(request):
    """Both modules carry their own copy -- they run in separate containers and
    share no code, so both are exercised."""
    return (database_functions.split_sql_statements if request.param == "local"
            else server_db.split_sql_statements)


class TestSplittingSchemaFiles:
    def test_splits_on_statement_boundaries(self, split):
        assert split("CREATE TABLE a (x INT); CREATE TABLE b (y INT)") == [
            "CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"]

    def test_a_semicolon_in_a_comment_does_not_split_a_statement(self, split):
        """The exact failure: a comment reading "...for approval; the pipeline
        applies it" cut the following CREATE TABLE in half."""
        sql = """-- Sent for approval; the pipeline applies whatever comes back.
CREATE TABLE proposals (id INTEGER PRIMARY KEY, name VARCHAR)"""
        statements = split(sql)
        assert len(statements) == 1
        assert statements[0].startswith("CREATE TABLE proposals")

    def test_a_trailing_comment_with_a_semicolon_is_stripped(self, split):
        sql = "CREATE TABLE a (\n  run_key VARCHAR  -- e.g. '2026-08'; one per month\n)"
        statements = split(sql)
        assert len(statements) == 1
        assert "one per month" not in statements[0]

    def test_a_double_dash_inside_a_string_literal_is_preserved(self, split):
        sql = "INSERT INTO a VALUES ('before -- after')"
        assert split(sql) == ["INSERT INTO a VALUES ('before -- after')"]

    def test_blank_lines_and_trailing_semicolons_produce_no_empty_statements(self, split):
        assert split("CREATE TABLE a (x INT);\n\n\n;  \n") == ["CREATE TABLE a (x INT)"]

    def test_the_real_schema_files_still_parse(self):
        """Guards against a future comment reintroducing the problem."""
        from pathlib import Path
        root = Path(__file__).parent.parent / "sql"
        for name, splitter in (("tables.sql", database_functions.split_sql_statements),
                               ("server_tables.sql", server_db.split_sql_statements)):
            con = duckdb.connect(":memory:")
            for statement in splitter((root / name).read_text()):
                con.execute(statement)
            assert con.execute("SHOW TABLES").fetchall(), name
            con.close()


class TestAdditiveMigrations:
    @pytest.fixture(params=["local", "server"])
    def apply(self, request):
        return (database_functions._apply_migrations if request.param == "local"
                else server_db._apply_migrations)

    def test_a_missing_column_is_added(self, apply):
        con = duckdb.connect(":memory:")
        con.execute("CREATE TABLE t (id INTEGER)")
        assert apply(con, [("t", "extra", "VARCHAR")]) == ["t.extra"]
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='t'").fetchall()]
        assert "extra" in cols

    def test_an_existing_column_is_left_alone_and_keeps_its_data(self, apply):
        """Safe to run on every startup -- it must never rewrite live data."""
        con = duckdb.connect(":memory:")
        con.execute("CREATE TABLE t (id INTEGER, extra VARCHAR)")
        con.execute("INSERT INTO t VALUES (1, 'keep me')")
        assert apply(con, [("t", "extra", "VARCHAR")]) == []
        assert con.execute("SELECT extra FROM t").fetchone()[0] == "keep me"

    def test_running_twice_is_a_no_op(self, apply):
        con = duckdb.connect(":memory:")
        con.execute("CREATE TABLE t (id INTEGER)")
        apply(con, [("t", "extra", "VARCHAR")])
        assert apply(con, [("t", "extra", "VARCHAR")]) == []

    def test_a_table_that_does_not_exist_yet_is_skipped(self, apply):
        """CREATE TABLE will include the column, so there is nothing to add and
        a missing table must not raise on a fresh database."""
        con = duckdb.connect(":memory:")
        assert apply(con, [("not_created_yet", "extra", "VARCHAR")]) == []

    def test_every_declared_local_migration_applies_to_a_fresh_database(self):
        """A typo in the table or column name would otherwise sit unnoticed
        until it was needed on a real upgrade."""
        con = duckdb.connect(":memory:")
        for statement in database_functions.split_sql_statements(
                (__import__("pathlib").Path(__file__).parent.parent / "sql" / "tables.sql").read_text()):
            con.execute(statement)
        for table, column, _ in database_functions.MIGRATIONS:
            cols = [r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=?",
                [table]).fetchall()]
            assert cols, f"{table} is not created by tables.sql"
            assert column in cols, f"{table}.{column} is missing from tables.sql"


class TestCategoryProposalsMultiOptionMigration:
    """One-off drop-and-recreate migration for category_proposals (see the
    function's own docstring for why this table, alone, gets an exception to
    the additive-only rule). Both the local and server copy get the same
    treatment, so both are exercised here."""

    def _old_shape(self, con):
        con.execute("""
            CREATE TABLE category_proposals (
                id INTEGER PRIMARY KEY, parent_name VARCHAR NOT NULL,
                parent_is_new BOOLEAN NOT NULL, subcategory_name VARCHAR NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'pending', proposed_at TIMESTAMP NOT NULL,
                decided_at TIMESTAMP, applied_at TIMESTAMP
            )
        """)

    def test_the_old_shape_is_dropped_and_recreated_with_the_new_columns(self):
        con = duckdb.connect(":memory:")
        self._old_shape(con)
        con.execute(
            """INSERT INTO category_proposals (id, parent_name, parent_is_new, subcategory_name, status, proposed_at)
               VALUES (1, 'Tax', TRUE, 'Self Assessment', 'applied', NOW())"""
        )
        database_functions._migrate_category_proposals_to_multi_option(con)
        cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'category_proposals'").fetchall()}
        assert "options" in cols and "selected_option" in cols
        assert "parent_name" not in cols
        assert con.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 0

    def test_a_pending_row_blocks_the_migration(self):
        """Refuses to silently destroy a proposal nobody has answered yet."""
        con = duckdb.connect(":memory:")
        self._old_shape(con)
        con.execute(
            """INSERT INTO category_proposals (id, parent_name, parent_is_new, subcategory_name, status, proposed_at)
               VALUES (1, 'Tax', TRUE, 'Self Assessment', 'pending', NOW())"""
        )
        database_functions._migrate_category_proposals_to_multi_option(con)
        cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'category_proposals'").fetchall()}
        assert "parent_name" in cols, "the old shape must be left untouched while a decision is outstanding"

    def test_running_twice_is_a_no_op(self):
        con = duckdb.connect(":memory:")
        self._old_shape(con)
        database_functions._migrate_category_proposals_to_multi_option(con)
        database_functions._migrate_category_proposals_to_multi_option(con)  # must not raise
        cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'category_proposals'").fetchall()}
        assert "options" in cols

    def test_a_database_already_on_the_new_shape_is_left_alone(self):
        """A fresh DB gets the new shape straight from tables.sql -- the
        migration must recognise that and do nothing."""
        con = duckdb.connect(":memory:")
        for statement in database_functions.split_sql_statements(
                (__import__("pathlib").Path(__file__).parent.parent / "sql" / "tables.sql").read_text()):
            con.execute(statement)
        con.execute(
            """INSERT INTO category_proposals (id, options, status, proposed_at)
               VALUES (1, '[]', 'applied', NOW())"""
        )
        database_functions._migrate_category_proposals_to_multi_option(con)
        assert con.execute("SELECT COUNT(*) FROM category_proposals").fetchone()[0] == 1

    def test_the_server_copy_behaves_the_same_way(self):
        import os
        con = duckdb.connect(":memory:")
        self._old_shape(con)
        con.execute(
            """INSERT INTO category_proposals (id, parent_name, parent_is_new, subcategory_name, status, proposed_at)
               VALUES (1, 'Tax', TRUE, 'Self Assessment', 'applied', NOW())"""
        )
        sql_path = os.path.join(os.path.dirname(server_db.__file__), "..", "..", "sql", "server_tables.sql")
        server_db._migrate_category_proposals_to_multi_option(con, sql_path)
        cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'category_proposals'").fetchall()}
        assert "options" in cols and "selected_option" in cols
        assert "parent_name" not in cols
