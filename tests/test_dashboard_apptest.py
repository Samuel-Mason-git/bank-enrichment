from pathlib import Path

import pytest
import database_functions
from streamlit.testing.v1 import AppTest

_DASHBOARD_PATH = str(
    Path(__file__).parent.parent / "src" / "local_scripts" / "dashboard.py"
)


def _seed(tmp_path):
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([{
        "id": "tx_001",
        "payload": {"data": {
            "amount": -999, "currency": "GBP", "description": "Weekly shop",
            "category": "groceries", "merchant": {"name": "Tesco"},
            "counterparty": {"name": ""}, "is_load": False,
            "created": "2026-01-15T10:00:00", "settled": "2026-01-15T10:00:00",
        }},
        "user_context": "weekly shop", "skipped": False,
        "received_at": "2026-01-15T10:00:00", "enriched_at": "2026-01-15T10:00:01",
    }])


def test_dashboard_loads_without_error(tmp_path):
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    assert not at.exception


def test_search_with_regex_special_characters_does_not_crash(tmp_path):
    """Regression test for the ArrowInvalid crash: typing a search term with an
    unbalanced parenthesis used to bring down the whole dashboard."""
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    at.sidebar.text_input[0].set_value("Tesco (UK").run()
    assert not at.exception


def test_subscription_delete_requires_confirmation(tmp_path):
    """Clicking the subscription delete button must not delete immediately —
    it should show a confirm/cancel step first."""
    _seed(tmp_path)
    database_functions.upsert_subscription("Netflix", 9.99, "monthly", "Netflix")

    at = AppTest.from_file(_DASHBOARD_PATH).run()
    subs_tab = at.tabs[5]  # Overview, Time, Txns, Drill, Merchants, Subs, Taxonomy
    delete_button = next(b for b in subs_tab.button if b.label == "✕")
    delete_button.click().run()

    assert not at.exception
    assert database_functions.get_subscriptions(), "subscription should not be deleted yet"
    assert any("Delete" in w.value for w in at.tabs[5].warning)


def test_savings_rate_shows_dash_when_no_income(tmp_path):
    """Regression test: with only spend and zero income in range, Savings Rate
    used to show a misleading '0.0%' (implying break-even) instead of '—'
    (undefined) — inconsistent with how the rest of the app handles undefined
    ratios (e.g. Category Drill-Down's 'Avg Spend' already used '—')."""
    _seed(tmp_path)  # spend-only seed data, no income transaction
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    overview_tab = at.tabs[0]
    savings_metric = next(m for m in overview_tab.metric if m.label == "Savings Rate")
    assert savings_metric.value == "—"


def test_time_tab_handles_zero_income_month_over_month(tmp_path):
    """Regression test: amount is DECIMAL(19,4) in DuckDB, so Spend/Income land
    in the Monthly Totals table as Python Decimal objects. Decimal division by
    zero raises decimal.DivisionByZero instead of producing inf/nan like float
    division does, which crashed pct_change() for any filtered view with a $0
    income month (e.g. a merchant that's pure spend, viewed across >1 month)."""
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([
        {
            "id": "tx_001",
            "payload": {"data": {
                "amount": -999, "currency": "GBP", "description": "Weekly shop",
                "category": "groceries", "merchant": {"name": "Tesco"},
                "counterparty": {"name": ""}, "is_load": False,
                "created": "2026-01-15T10:00:00", "settled": "2026-01-15T10:00:00",
            }},
            "user_context": "weekly shop", "skipped": False,
            "received_at": "2026-01-15T10:00:00", "enriched_at": "2026-01-15T10:00:01",
        },
        {
            "id": "tx_002",
            "payload": {"data": {
                "amount": -500, "currency": "GBP", "description": "Weekly shop",
                "category": "groceries", "merchant": {"name": "Tesco"},
                "counterparty": {"name": ""}, "is_load": False,
                "created": "2026-02-15T10:00:00", "settled": "2026-02-15T10:00:00",
            }},
            "user_context": "weekly shop", "skipped": False,
            "received_at": "2026-02-15T10:00:00", "enriched_at": "2026-02-15T10:00:01",
        },
    ])

    at = AppTest.from_file(_DASHBOARD_PATH).run()
    time_tab = at.tabs[1]  # Overview, Time, Txns, Drill, Merchants, Subs, Taxonomy
    assert any("Monthly Totals" in h.value for h in time_tab.subheader)
    assert not at.exception


def test_subscription_can_be_edited(tmp_path):
    """Regression test: subscriptions previously had no update path at all —
    upsert_subscription() is insert-only, so the only way to change a price
    was delete-and-recreate. Editing must update the existing row in place."""
    _seed(tmp_path)
    database_functions.upsert_subscription("Netflix", 9.99, "monthly", "Netflix")

    at = AppTest.from_file(_DASHBOARD_PATH).run()
    subs_tab = at.tabs[5]
    edit_button = next(b for b in subs_tab.button if b.label == "✏️")
    edit_button.click().run()

    # number_input[0] is the "Add Subscription" form's amount field (always
    # rendered); the edit form's amount field is the second one on the page.
    at.tabs[5].number_input[1].set_value(12.99).run()
    save_button = next(b for b in at.tabs[5].button if b.label == "Save")
    save_button.click().run()

    assert not at.exception
    subs = database_functions.get_subscriptions()
    assert len(subs) == 1, "editing must not create a duplicate row"
    assert float(subs[0]["amount"]) == pytest.approx(12.99)
    assert subs[0]["name"] == "Netflix"
