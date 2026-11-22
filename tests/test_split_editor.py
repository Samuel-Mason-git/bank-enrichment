"""The split editor on the payment details page: fill in the parts, save, and the split is stored
with its parts counted the way the category pages count them."""
import streamlit as st
from pathlib import Path

from streamlit.testing.v1 import AppTest

import database_functions as dbf
from tests.local_helpers import ensure_category, insert_transaction, label

_DASHBOARD_PATH = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def _tesco(db):
    ensure_category(db, "Food & Drink", "Groceries")
    ensure_category(db, "Food & Drink", "Coffee Shops")
    ensure_category(db, "Personal Care & Consumables", "Toiletries")
    insert_transaction(db, "tesco", amount=-50.0, description="Tesco", merchant_name="Tesco",
                       created_at="2026-08-10 12:00:00")
    label(db, "tesco", "Food & Drink", "Groceries")


def _page() -> AppTest:
    st.cache_data.clear()
    at = AppTest.from_file(_DASHBOARD_PATH, default_timeout=30)
    at.switch_page("views/payment.py")
    at.run()
    at.selectbox(key="payment_id").set_value("tesco").run()
    return at


def test_saving_a_split_stores_its_parts_and_the_rest(db):
    _tesco(db)
    at = _page()
    at.selectbox(key="split_cat_0").set_value("Personal Care & Consumables › Toiletries")
    at.text_input(key="split_amt_0").set_value("20")
    at.selectbox(key="split_cat_1").set_value("Food & Drink › Coffee Shops")
    at.text_input(key="split_amt_1").set_value("10")
    at.selectbox(key="split_cat_2").set_value("Food & Drink › Groceries")
    at.text_input(key="split_amt_2").set_value("")          # takes the rest
    at.button(key="split_save").click().run()

    assert not at.exception
    lines = dbf.get_split("tesco")
    assert sorted(l["pence"] for l in lines) == [1000, 2000, 2000]
    assert sum(l["pence"] for l in lines) == 5000


def test_a_split_that_does_not_add_up_says_so_and_saves_nothing(db):
    _tesco(db)
    at = _page()
    at.selectbox(key="split_cat_0").set_value("Food & Drink › Groceries")
    at.text_input(key="split_amt_0").set_value("30")
    at.selectbox(key="split_cat_1").set_value("Food & Drink › Coffee Shops")
    at.text_input(key="split_amt_1").set_value("10")
    at.button(key="split_save").click().run()

    assert any("still to be given" in e.value for e in at.error)
    assert dbf.get_split("tesco") == []


def test_clearing_the_split_puts_the_payment_back(db):
    _tesco(db)
    parent, groceries = ensure_category(db, "Food & Drink", "Groceries")
    _, coffee = ensure_category(db, "Food & Drink", "Coffee Shops")
    dbf.set_split("tesco", [{"parent_id": parent, "subcategory_id": groceries, "pence": 3000},
                            {"parent_id": parent, "subcategory_id": coffee, "pence": 2000}], source="dashboard")
    at = _page()
    at.button(key="split_clear").click().run()
    assert dbf.get_split("tesco") == []
