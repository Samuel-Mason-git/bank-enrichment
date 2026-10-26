"""New rows take the next id up, and two writers taking one at the same moment
(the worker, a dashboard edit, the server approving a rule) don't collide."""
import os
import threading
import time

import psycopg
import pytest

import database_functions as dbf
import server_db


def _second_connection(pg):
    schema = pg.execute("SELECT current_schema()").fetchone()[0]
    con = psycopg.connect(os.environ["TEST_POSTGRES_URL"], autocommit=True)
    con.execute(f"SET search_path TO {schema}")
    return con


SQL = ("INSERT INTO subscriptions (id, name, amount, frequency, active, created_at) "
       "VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM subscriptions), %s, 1, 'monthly', TRUE, NOW()) RETURNING id")


@pytest.mark.parametrize("helper", [dbf.insert_with_next_id, server_db.insert_with_next_id])
def test_a_clash_on_the_id_takes_the_next_one(pg, helper):
    """The first writer has taken id 1 but not committed; the second picks 1
    too, waits, and clashes when the first commits -- then takes 2."""
    first = _second_connection(pg)
    second = _second_connection(pg)
    first.autocommit = False
    first.execute(SQL, ["first"])  # id 1, not committed yet
    result = {}

    def write():
        result["id"] = helper(second, SQL, ["second"], "subscriptions")

    thread = threading.Thread(target=write)
    thread.start()
    time.sleep(0.5)  # the second is now waiting on the first's id
    first.commit()
    thread.join(timeout=10)
    assert result["id"] == 2
    assert pg.execute("SELECT id, name FROM subscriptions ORDER BY id").fetchall() == [(1, "first"), (2, "second")]
    first.close()
    second.close()


def test_a_clash_on_another_rule_is_not_retried(pg):
    """A name already taken is a real error, not a race."""
    pg.execute("INSERT INTO parent_categories (id, name, created_at) VALUES (1, 'Food', NOW())")
    sql = ("INSERT INTO parent_categories (id, name, created_at) "
           "VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM parent_categories), %s, NOW()) RETURNING id")
    with pytest.raises(psycopg.errors.UniqueViolation):
        dbf.insert_with_next_id(pg, sql, ["Food"], "parent_categories")
    assert pg.execute("SELECT COUNT(*) FROM parent_categories").fetchone()[0] == 1


def test_ids_continue_after_rows_written_with_their_own(db, pg):
    """Rows copied in with explicit ids (as at the switch) don't trip it up."""
    pg.execute("INSERT INTO subscriptions (id, name, amount, frequency, active, created_at) "
               "VALUES (41, 'Old', 5, 'monthly', TRUE, NOW())")
    assert dbf.upsert_subscription("New", 9.99, "monthly") == 42
