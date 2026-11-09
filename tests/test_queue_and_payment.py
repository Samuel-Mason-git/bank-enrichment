"""The dashboard's Queue and Payment pages: what they read (the queue, one
payment's history) and what they write (context, skip, unskip, delete) --
written the way the server writes them."""
from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

import database_functions as dbf
from tests.local_helpers import ensure_category, hold, insert_transaction, row

_DASHBOARD_PATH = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def _ask(con, txn_id: str, times: int = 1) -> None:
    for _ in range(times):
        con.execute("INSERT INTO context_events (txn_id, source, set_at) VALUES (%s, 'request', NOW())", [txn_id])


def _rule_label(con, txn_id: str) -> None:
    parent_id, sub_id = ensure_category(con, "Food & Drink", "Groceries")
    con.execute("""INSERT INTO label_events (txn_id, parent_id, subcategory_id, source, set_at)
                   VALUES (%s, %s, %s, 'rule', NOW())""", [txn_id, parent_id, sub_id])


def _seed(con) -> None:
    dbf._seed_taxonomy()
    insert_transaction(con, "tx_waiting", amount=-12.5, merchant_name="Tesco", answered=False)
    insert_transaction(con, "tx_answered", amount=-30, merchant_name="Boots", user_context="Plasters",
                       created_at="2026-01-16T10:00:00")
    insert_transaction(con, "tx_done", amount=-5, merchant_name="Greggs", user_context="Lunch",
                       llm_category="Food & Drink", llm_subcategory="Eating Out",
                       created_at="2026-01-17T10:00:00")
    _ask(con, "tx_waiting", times=2)


# ── Writing ───────────────────────────────────────────────────────────────────

def test_set_context_is_a_dashboard_context_event(db):
    _seed(db)
    dbf.set_context("tx_waiting", "Weekly shop")
    t = row(db, "tx_waiting")
    assert t["user_context"] == "Weekly shop" and not t["skipped"]
    assert db.execute("SELECT source FROM context_events WHERE txn_id = 'tx_waiting' "
                      "ORDER BY event_id DESC LIMIT 1").fetchone()[0] == "dashboard"


def test_set_context_carries_a_corrected_date_forward(db):
    _seed(db)
    dbf.update_transaction_details("tx_answered", "2026-01-20 09:00:00", "Plasters")
    dbf.set_context("tx_answered", "First aid")
    assert str(row(db, "tx_answered")["created_at"]).startswith("2026-01-20 09:00")


def test_set_context_clears_a_rule_label_but_not_others(db):
    _seed(db)
    _rule_label(db, "tx_answered")
    dbf.set_context("tx_answered", "Actually a gift")
    assert row(db, "tx_answered")["llm_subcategory"] is None, "a rule's label gives way to your answer"

    dbf.set_context("tx_done", "Lunch with Sam")
    assert row(db, "tx_done")["llm_subcategory"] == "Eating Out", "a classifier's label stays"


def test_skip_and_unskip(db):
    _seed(db)
    dbf.skip_transaction("tx_waiting")
    assert row(db, "tx_waiting")["skipped"]
    dbf.unskip_transaction("tx_waiting")
    t = row(db, "tx_waiting")
    assert not t["skipped"] and t["user_context"] is None


def test_delete_takes_the_whole_history(db):
    _seed(db)
    dbf.delete_transaction("tx_waiting")
    for table, column in (("transactions_raw", "id"), ("context_events", "txn_id"), ("label_events", "txn_id")):
        assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = 'tx_waiting'").fetchone()[0] == 0
    assert row(db, "tx_answered") is not None


# ── Reading ───────────────────────────────────────────────────────────────────

def test_queue_statuses_and_times_asked(db):
    _seed(db)
    dbf.skip_transaction("tx_answered")
    by_id = {t["id"]: t for t in dbf.get_queue_transactions()}
    assert by_id["tx_waiting"]["status"] == "pending"
    assert by_id["tx_waiting"]["times_asked"] == 2
    assert by_id["tx_answered"]["status"] == "skipped"
    assert by_id["tx_done"]["status"] == "classified"


def test_payment_history_tells_the_story_in_order(db):
    _seed(db)
    dbf.set_context("tx_waiting", "Weekly shop")
    _rule_label(db, "tx_waiting")
    kinds = [h["kind"] for h in dbf.get_payment_history("tx_waiting")]
    assert kinds[0] == "arrived"
    assert kinds.count("asked") == 2
    assert kinds.index("context") < kinds.index("labelled")
    labelled = next(h for h in dbf.get_payment_history("tx_waiting") if h["kind"] == "labelled")
    assert labelled["detail"] == "Food & Drink › Groceries" and labelled["source"] == "rule"


def test_lifetime_stats(db):
    _seed(db)
    stats = dbf.get_lifetime_stats()
    assert stats == {"received": 3, "asked": 2, "answered": 2, "classified": 1}


# ── The pages ─────────────────────────────────────────────────────────────────

def _app(page: str) -> AppTest:
    st.cache_data.clear()
    at = AppTest.from_file(_DASHBOARD_PATH, default_timeout=30)
    at.switch_page(f"views/{page}.py")
    return at.run()


def test_queue_page_shows_what_is_waiting(db):
    _seed(db)
    at = _app("queue")
    assert not at.exception
    cards = " ".join(md.value for md in at.markdown)
    assert "Needs your answer" in cards and "Waiting for the classifier" in cards
    table = at.dataframe[0].value
    assert set(table["Payee"]) == {"Tesco", "Boots"}
    assert table.loc[table["Payee"] == "Tesco", "Times asked"].item() == 2


def test_payment_page_shows_history_and_answers(db):
    _seed(db)
    at = _app("payment")
    at.selectbox(key="payment_id").set_value("tx_waiting").run()
    assert not at.exception
    assert any("Tesco" in s.value for s in at.subheader)
    history = at.dataframe[2].value
    assert list(history["What"][:3]) == ["Arrived from Monzo", "Asked you about it", "Asked you about it"]

    at.text_input(key="payment_ctx_tx_waiting").set_value("Weekly shop")
    at.button(key="payment_save_tx_waiting").click().run()
    assert not at.exception
    assert row(db, "tx_waiting")["user_context"] == "Weekly shop"


def test_payment_page_skips_and_deletes_with_a_confirm(db):
    _seed(db)
    at = _app("payment")
    at.selectbox(key="payment_id").set_value("tx_answered").run()
    at.button(key="payment_skip_tx_answered").click().run()
    assert row(db, "tx_answered")["skipped"]

    at.button(key="payment_del_tx_answered").click().run()
    assert row(db, "tx_answered") is not None, "not deleted before confirming"
    at.button(key="payment_del_yes_tx_answered").click().run()
    assert not at.exception
    assert row(db, "tx_answered") is None
    assert at.selectbox(key="payment_id").value == ""


def test_a_payment_held_for_a_new_category_says_so(db):
    _seed(db)
    db.execute("INSERT INTO category_proposals (id, options, proposed_at) VALUES (1, '[]', NOW())")
    hold(db, 1, ["tx_answered"])
    by_id = {t["id"]: t for t in dbf.get_queue_transactions()}
    assert by_id["tx_answered"]["status"] == "held"


def test_history_shows_a_new_category_suggestion(db):
    _seed(db)
    db.execute("""INSERT INTO category_proposals (id, options, proposed_at) VALUES
                  (1, '[{"parent_name": "Government & Compliance", "subcategory_name": "Government Fees"}]', NOW())""")
    hold(db, 1, ["tx_answered"])
    proposed = next(h for h in dbf.get_payment_history("tx_answered") if h["kind"] == "proposed")
    assert proposed["detail"] == "Government & Compliance › Government Fees"
    assert proposed["extra"] == "pending"
