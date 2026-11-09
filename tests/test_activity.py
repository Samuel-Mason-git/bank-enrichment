"""The Activity log: runs and warnings/errors recorded in the database
(src/pipeline/activity.py), what the dashboard reads back, and its page."""
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import activity
import database_functions as dbf
import process
from tests.local_helpers import insert_transaction

_DASHBOARD_PATH = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


@pytest.fixture(autouse=True)
def _no_leftover_handler():
    """attach() adds to the root logger, which outlives a test: take it off."""
    yield
    root = logging.getLogger()
    for h in [h for h in root.handlers if isinstance(h, activity.DatabaseLogHandler)]:
        root.removeHandler(h)
    process._db_log = None


def _events(con):
    return con.execute("SELECT service, level, logger, message, detail FROM log_events ORDER BY event_id").fetchall()


# ── The handler ───────────────────────────────────────────────────────────────

def test_warnings_and_errors_are_saved_but_not_info(db):
    activity.attach("worker", lambda: db)
    test_log = logging.getLogger("test.activity")
    test_log.info("routine")
    test_log.warning("odd")
    try:
        raise ValueError("bad")
    except ValueError:
        test_log.error("broke", exc_info=True)
    rows = _events(db)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("worker", "WARNING", "test.activity", "odd"), ("worker", "ERROR", "test.activity", "broke")]
    assert "ValueError: bad" in rows[1][4]


def test_a_database_that_cant_take_it_doesnt_break_logging(db):
    def broken():
        raise OSError("database gone")
    activity.attach("server", broken)
    logging.getLogger("test.activity").error("still fine")  # no exception


def test_attach_twice_keeps_one_handler(db):
    activity.attach("server", lambda: db)
    activity.attach("server", lambda: db)
    logging.getLogger("test.activity").warning("once")
    assert len(_events(db)) == 1


# ── Runs ──────────────────────────────────────────────────────────────────────

def test_a_run_is_recorded_with_what_it_did(db):
    dbf._seed_taxonomy()
    insert_transaction(db, "tx_1", merchant_name="Tesco", user_context="Food")

    def steps():
        parent_id, sub_id = dbf.label_ids("Food & Drink", "Groceries")
        db.execute("""INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, set_at)
                      VALUES ('tx_1', %s, %s, 'llm', NOW())""", [parent_id, sub_id])

    with patch.object(process, "_steps", side_effect=steps):
        assert process.main() == process.RAN
    run = db.execute("SELECT status, summary, finished_at IS NOT NULL FROM runs").fetchone()
    assert run == ("ok", {"labelled_llm": 1}, True)


def test_a_step_error_marks_the_run_as_having_problems(db):
    with patch.object(process, "_steps", side_effect=lambda: process.log.error("Rule suggestions failed")):
        process.main()
    status, summary = db.execute("SELECT status, summary FROM runs").fetchone()
    assert status == "problems" and summary == {"errors": 1}
    assert _events(db)[0][:2] == ("worker", "ERROR")


def test_a_run_that_couldnt_start_is_recorded_as_skipped(db, pg):
    pg.execute("SELECT pg_advisory_lock(hashtext(%s))", [process.RUN_LOCK])
    with patch.object(process, "_steps"):
        assert process.main() == process.ALREADY_RUNNING
    pg.execute("SELECT pg_advisory_unlock(hashtext(%s))", [process.RUN_LOCK])
    assert db.execute("SELECT status FROM runs").fetchone()[0] == "skipped"


def test_old_activity_is_cleared(db):
    db.execute("INSERT INTO runs (started_at, status) VALUES (now() - INTERVAL '100 days', 'ok'), (now(), 'ok')")
    db.execute("INSERT INTO log_events (logged_at, service, level, message) VALUES "
               "(now() - INTERVAL '100 days', 'server', 'ERROR', 'old'), (now(), 'server', 'ERROR', 'new')")
    activity.clear_old(db)
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert db.execute("SELECT message FROM log_events").fetchall() == [("new",)]


# ── Reading a day ─────────────────────────────────────────────────────────────

def test_a_days_runs_problems_and_events_in_london_time(db):
    dbf._seed_taxonomy()
    # 23:30 UTC on 30 June is 00:30 on 1 July in London.
    late = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)
    db.execute("INSERT INTO runs (started_at, finished_at, status) VALUES (%s, %s, 'ok')", [late, late])
    db.execute("INSERT INTO log_events (logged_at, service, level, message) VALUES (%s, 'server', 'WARNING', 'x')",
               [late])
    insert_transaction(db, "tx_1", merchant_name="Tesco", amount=-4.5, created_at="2026-07-01T09:00:00",
                       received_at="2026-07-01T09:00:00", answered=False)
    db.execute("INSERT INTO context_events (txn_id, source, set_at) VALUES ('tx_1', 'request', '2026-07-01T09:01:00Z')")

    assert len(dbf.get_runs(date(2026, 7, 1))) == 1 and dbf.get_runs(date(2026, 6, 30)) == []
    assert [p["message"] for p in dbf.get_log_events(date(2026, 7, 1))] == ["x"]
    events = dbf.get_day_events(date(2026, 7, 1))
    assert [(e["kind"], e["payee"], float(e["amount"])) for e in events] == [
        ("arrived", "Tesco", -4.5), ("asked", "Tesco", -4.5)]


def test_activity_by_day_covers_every_day(db):
    days = dbf.get_activity_by_day(7)
    assert len(days) == 7 and all(d["runs"] == 0 for d in days)


# ── The page ──────────────────────────────────────────────────────────────────

def test_activity_page_shows_a_day(db):
    dbf._seed_taxonomy()
    insert_transaction(db, "tx_1", merchant_name="Tesco", user_context="Food")
    db.execute("INSERT INTO runs (started_at, finished_at, status, summary) "
               "VALUES (now(), now(), 'problems', '{\"labelled_llm\": 2, \"errors\": 1}')")
    db.execute("INSERT INTO log_events (service, level, logger, message, detail) "
               "VALUES ('worker', 'ERROR', 'process', 'Rule suggestions failed', 'Traceback...')")
    st.cache_data.clear()
    at = AppTest.from_file(_DASHBOARD_PATH, default_timeout=30)
    at.switch_page("views/activity.py")
    at.run()

    assert not at.exception
    runs = at.dataframe[0].value
    assert runs["What it did"].item() == "2 labelled by the classifier · 1 error"
    assert at.dataframe[1].value["Message"].item() == "Rule suggestions failed"
