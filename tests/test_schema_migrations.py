"""Schema loading and additive migrations, for both databases.

Two silent-failure modes motivated these. A semicolon inside a comment used to
cut a CREATE TABLE in half, so the whole schema load failed on a fragment. And
CREATE TABLE IF NOT EXISTS is a no-op once a table exists, so a column added to
the .sql file never reached a database that already had that table -- on the
server that meant a deploy appearing to succeed and the app then failing at
runtime against live webhooks.
"""
from pathlib import Path

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
    # The local DuckDB's. The server's Postgres schema adds columns with
    # ADD COLUMN IF NOT EXISTS in schema.sql instead (test_postgres_schema).
    @pytest.fixture
    def apply(self):
        return database_functions._apply_migrations

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


class TestCategoryRolesCheckConstraintMigration:
    """DuckDB has no ALTER TABLE support for modifying a CHECK constraint at
    all (confirmed directly: ALTER TABLE ... DROP CONSTRAINT raises "No
    support for that ALTER TABLE option yet!" on the DuckDB version this
    project uses) -- so adding the 'saving' role needed a different mechanism
    than MIGRATIONS/_apply_migrations (ADD COLUMN only): rename the table
    aside, create a new one with the wider constraint, copy every row across,
    drop the old one."""

    def _old_schema_db(self):
        """A database shaped like one created before 'saving' existed --
        category_roles with the original 5-role CHECK constraint."""
        con = duckdb.connect(":memory:")
        con.execute("""
            CREATE TABLE parent_categories (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL UNIQUE, created_at TIMESTAMP NOT NULL);
            CREATE TABLE subcategories (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, parent_id INTEGER NOT NULL REFERENCES parent_categories(id), created_at TIMESTAMP NOT NULL, UNIQUE (name, parent_id));
            CREATE TABLE category_roles (
                parent_id      INTEGER NOT NULL REFERENCES parent_categories(id),
                subcategory_id INTEGER REFERENCES subcategories(id),
                role           VARCHAR NOT NULL CHECK (role IN ('income', 'spend', 'investment', 'transfer', 'excluded')),
                UNIQUE (parent_id, subcategory_id)
            );
            INSERT INTO parent_categories VALUES (1, 'Investments', NOW());
            INSERT INTO category_roles VALUES (1, NULL, 'investment');
        """)
        return con

    def _migrate(self, con):
        database_functions._con = con
        database_functions._migrate_category_roles_roles()

    def test_a_pre_saving_role_database_is_migrated(self):
        con = self._old_schema_db()
        self._migrate(con)
        con.execute("INSERT INTO parent_categories VALUES (2, 'Savings', NOW())")
        con.execute("INSERT INTO category_roles VALUES (2, NULL, 'saving')")  # must not raise
        assert con.execute("SELECT role FROM category_roles WHERE parent_id = 2").fetchone()[0] == "saving"

    def test_existing_role_data_survives_untouched(self):
        con = self._old_schema_db()
        self._migrate(con)
        assert con.execute("SELECT parent_id, subcategory_id, role FROM category_roles").fetchall() == [
            (1, None, "investment")]

    def test_an_invalid_role_is_still_rejected_after_migrating(self):
        con = self._old_schema_db()
        self._migrate(con)
        con.execute("INSERT INTO parent_categories VALUES (3, 'Nonsense', NOW())")
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO category_roles VALUES (3, NULL, 'not_a_real_role')")

    def test_running_twice_is_a_no_op(self):
        con = self._old_schema_db()
        self._migrate(con)
        self._migrate(con)  # must not raise, and must not touch data again
        assert con.execute("SELECT parent_id, subcategory_id, role FROM category_roles").fetchall() == [
            (1, None, "investment")]
        assert con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'category_roles_pre_saving_role'"
        ).fetchone()[0] == 0, "the temporary rename target must never be left lying around"

    def test_a_fresh_database_from_tables_sql_already_accepts_saving_and_needs_no_migration(self):
        """tables.sql's own CREATE TABLE already lists 'saving' -- a brand new
        database is already correct, and the migration must recognise that and
        do nothing rather than attempt a pointless rename/copy."""
        con = duckdb.connect(":memory:")
        for statement in database_functions.split_sql_statements(
                (Path(__file__).parent.parent / "sql" / "tables.sql").read_text()):
            con.execute(statement)
        database_functions._con = con
        database_functions._migrate_category_roles_roles()
        con.execute("INSERT INTO parent_categories (id, name, created_at) VALUES (99, 'X', NOW())")
        con.execute("INSERT INTO category_roles VALUES (99, NULL, 'saving')")  # already worked before migrate() ran
        assert con.execute("SELECT COUNT(*) FROM category_roles WHERE role = 'saving'").fetchone()[0] == 1

    def test_the_constraint_reader_returns_none_when_the_table_does_not_exist(self):
        con = duckdb.connect(":memory:")
        assert database_functions._category_roles_check_constraint(con) is None
