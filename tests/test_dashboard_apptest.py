from datetime import date
from pathlib import Path

import pytest
import streamlit as st
import database_functions
from streamlit.testing.v1 import AppTest

_DASHBOARD_PATH = str(
    Path(__file__).parent.parent / "src" / "local_scripts" / "dashboard.py"
)


def _has_card(overview_tab, label: str, value: str) -> bool:
    """Overview's Standard/Actual metric cards are custom HTML (st.markdown),
    not a real dataframe/metric widget -- each card's label and value live in
    the same markdown element's rendered HTML, so matching both substrings
    together identifies one specific card (Standard vs Actual for the same
    label always differ in value, since that's the whole point of the row)."""
    return any(label in md.value and value in md.value for md in overview_tab.markdown)


@pytest.fixture(autouse=True)
def _clear_streamlit_cache():
    """load_transactions()/load_taxonomy() are @st.cache_data-decorated with no
    arguments, and that cache is process-global -- not scoped per AppTest
    instance. Without clearing it, one test's seeded DB contents can silently
    leak into the next test's AppTest run since the cache key never changes."""
    st.cache_data.clear()
    yield


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
    subs_tab = at.tabs[5]  # Overview, Time, Txns, Drill, Merchants, Subs, Settings
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
    assert _has_card(overview_tab, "Savings Rate", "—")


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


def test_settings_tab_shows_default_category_roles(tmp_path):
    _seed(tmp_path)  # init_db() seeds the full default taxonomy
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    settings_tab = at.tabs[6]  # Overview, Time, Txns, Drill, Merchants, Subs, Settings

    parents = {p["name"]: p for p in database_functions.get_parents()}
    role_selectboxes = {sb.key: sb.value for sb in settings_tab.selectbox if sb.key.startswith("role_") and not sb.key.startswith("role_sub_")}
    assert role_selectboxes[f"role_{parents['Income']['id']}"] == "income"
    assert role_selectboxes[f"role_{parents['Investments']['id']}"] == "investment"
    assert role_selectboxes[f"role_{parents['Transfers']['id']}"] == "transfer"
    assert role_selectboxes[f"role_{parents['Bills & Utilities']['id']}"] == "spend"


def test_settings_tab_role_edit_persists(tmp_path):
    """Regression test for a real ordering bug found while writing an earlier
    version of this test (a data_editor grid, since replaced by a selectbox
    per category): get_parents() previously ordered only by transaction_count
    DESC with no tiebreaker, so rows with equal counts (the common case)
    could return in an unstable order across reruns."""
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    settings_tab = at.tabs[6]

    parents_before = {p["name"]: p for p in database_functions.get_parents()}
    bills_id = parents_before["Bills & Utilities"]["id"]
    role_select = next(sb for sb in settings_tab.selectbox if sb.key == f"role_{bills_id}")
    role_select.set_value("excluded").run()

    assert not at.exception
    parents = {p["name"]: p for p in database_functions.get_parents()}
    assert parents["Bills & Utilities"]["role"] == "excluded"
    assert parents["Income"]["role"] == "income", "unrelated categories must not be affected"


def test_settings_tab_subcategory_override_persists(tmp_path):
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    settings_tab = at.tabs[6]

    subs_before = {s["name"]: s for s in database_functions.get_subcategories() if s["parent_name"] == "Income"}
    refunds_id = subs_before["Refunds"]["id"]
    assert subs_before["Refunds"]["role_override"] == "excluded", "Refunds should default to excluded"

    override_select = next(sb for sb in settings_tab.selectbox if sb.key == f"role_sub_{refunds_id}")
    override_select.set_value("income").run()

    assert not at.exception
    subs_after = {s["name"]: s for s in database_functions.get_subcategories() if s["parent_name"] == "Income"}
    assert subs_after["Refunds"]["role_override"] == "income"


def test_settings_rename_subcategory_to_existing_name_shows_error_not_crash(tmp_path):
    """Regression test: subcategories has a UNIQUE(name, parent_id) constraint
    -- renaming one to a name already used by another subcategory under the
    same parent used to crash with an uncaught DuckDB constraint violation
    instead of a friendly error."""
    _seed(tmp_path)
    subs = {s["name"]: s for s in database_functions.get_subcategories() if s["parent_name"] == "Food & Drink"}
    groceries_id = subs["Groceries"]["id"]

    at = AppTest.from_file(_DASHBOARD_PATH).run()
    settings_tab = at.tabs[6]
    edit_btn = next(b for b in settings_tab.button if b.key == f"es_{groceries_id}")
    edit_btn.click().run()

    settings_tab2 = at.tabs[6]
    name_input = next(ti for ti in settings_tab2.text_input if ti.label == "Name")
    name_input.set_value("Restaurants")  # already exists under Food & Drink
    parent_select = next(sb for sb in settings_tab2.selectbox if sb.label == "Parent category")
    parent_select.set_value(parent_select.value)
    save_btn = next(b for b in settings_tab2.button if b.label == "Save" and b.proto.type == "primary")
    save_btn.click().run()

    assert not at.exception
    settings_tab3 = at.tabs[6]
    assert any("already exists" in e.value for e in settings_tab3.error)
    subs_after = {s["id"]: s["name"] for s in database_functions.get_subcategories()}
    assert subs_after[groceries_id] == "Groceries", "the rename must not have gone through"


def test_settings_rename_parent_carries_over_custom_role(tmp_path):
    """Regression test: renaming a parent category re-creates it under a new
    id (DuckDB FK constraints block updating a referenced row's name in
    place) -- the old id's role override used to get left behind, silently
    resetting a custom category's role back to its default on every rename."""
    _seed(tmp_path)
    parents = {p["name"]: p for p in database_functions.get_parents()}
    health_id = parents["Health"]["id"]
    database_functions.set_parent_role(health_id, "excluded")

    at = AppTest.from_file(_DASHBOARD_PATH).run()
    settings_tab = at.tabs[6]
    rename_btn = next(b for b in settings_tab.button if b.key == f"ep_{health_id}")
    rename_btn.click().run()

    settings_tab2 = at.tabs[6]
    name_input = next(ti for ti in settings_tab2.text_input if ti.label == "New name")
    name_input.set_value("Health & Wellness")
    save_btn = next(b for b in settings_tab2.button if b.label == "Save" and b.proto.type == "primary")
    save_btn.click().run()

    assert not at.exception
    parents_after = {p["name"]: p for p in database_functions.get_parents()}
    assert "Health & Wellness" in parents_after
    assert parents_after["Health & Wellness"]["role"] == "excluded"


def test_settings_rename_parent_to_existing_name_shows_error_not_silent_merge(tmp_path):
    """Regression test: renaming a parent to a name that already belongs to a
    different parent used to silently merge the two categories together (via
    upsert_parent's get-or-create), re-pointing all of the renamed parent's
    subcategories and transactions into the other one with zero warning."""
    _seed(tmp_path)
    parents = {p["name"]: p for p in database_functions.get_parents()}
    health_id = parents["Health"]["id"]

    at = AppTest.from_file(_DASHBOARD_PATH).run()
    settings_tab = at.tabs[6]
    rename_btn = next(b for b in settings_tab.button if b.key == f"ep_{health_id}")
    rename_btn.click().run()

    settings_tab2 = at.tabs[6]
    name_input = next(ti for ti in settings_tab2.text_input if ti.label == "New name")
    name_input.set_value("Income")  # already exists as a different parent
    save_btn = next(b for b in settings_tab2.button if b.label == "Save" and b.proto.type == "primary")
    save_btn.click().run()

    assert not at.exception
    settings_tab3 = at.tabs[6]
    assert any("already exists" in e.value for e in settings_tab3.error)
    parents_after = {p["name"] for p in database_functions.get_parents()}
    assert "Health" in parents_after, "Health must still exist, unmerged"


def test_overview_actual_income_excludes_refunds(tmp_path):
    """Regression test: the comparison table's "Standard" Income counts every
    positive-amount transaction, including refunds. The role-based "Actual"
    Income column should exclude them by default."""
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()  # seeds default taxonomy incl. Refunds -> excluded
    database_functions.write_to_db([
        {
            "id": "tx_refund", "payload": {"data": {
                "amount": 500, "currency": "GBP", "description": "Refund",
                "category": "eating_out", "merchant": {"name": "Caffe Nero"},
                "counterparty": {"name": ""}, "is_load": False,
                "created": "2026-01-10T10:00:00", "settled": "2026-01-10T10:00:00",
            }},
            "user_context": "refund", "skipped": False,
            "received_at": "2026-01-10T10:00:00", "enriched_at": "2026-01-10T10:00:01",
        },
        {
            "id": "tx_salary", "payload": {"data": {
                "amount": 200000, "currency": "GBP", "description": "Salary",
                "category": "income", "merchant": None,
                "counterparty": {"name": ""}, "is_load": False,
                "created": "2026-01-11T10:00:00", "settled": "2026-01-11T10:00:00",
            }},
            "user_context": "salary", "skipped": False,
            "received_at": "2026-01-11T10:00:00", "enriched_at": "2026-01-11T10:00:01",
        },
    ])
    database_functions.update_classification("tx_refund", "Income", "Refunds", 1.0, "test")
    database_functions.update_classification("tx_salary", "Income", "Salary", 1.0, "test")

    at = AppTest.from_file(_DASHBOARD_PATH).run()
    overview_tab = at.tabs[0]  # "All time" is the default preset, covers both seeded dates

    assert _has_card(overview_tab, "Income", "£2005.00")
    assert _has_card(overview_tab, "Income", "£2000.00")


def test_overview_chart_metric_selector_switches_without_crash(tmp_path):
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    overview_tab = at.tabs[0]
    metric_select = next(sb for sb in overview_tab.selectbox if sb.key == "overview_outgoing_metric")
    metric_select.set_value("Invested").run()
    assert not at.exception


def test_category_drilldown_shows_invested_not_spend_for_investments(tmp_path):
    """Regression test: Category Drill-Down used to compute Total Spend/Total
    Income purely from amount sign, so drilling into "Investments" (a negative
    amount) showed a misleading "Total Spend" figure instead of "Invested"."""
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([{
        "id": "tx_isa", "payload": {"data": {
            "amount": -30000, "currency": "GBP", "description": "ISA",
            "category": "investment", "merchant": None,
            "counterparty": {"name": ""}, "is_load": False,
            "created": "2026-01-10T10:00:00", "settled": "2026-01-10T10:00:00",
        }},
        "user_context": "isa", "skipped": False,
        "received_at": "2026-01-10T10:00:00", "enriched_at": "2026-01-10T10:00:01",
    }])
    database_functions.update_classification(
        "tx_isa", "Investments", "Stocks & Shares ISA Contributions", 1.0, "test"
    )

    at = AppTest.from_file(_DASHBOARD_PATH).run()
    drill_tab = at.tabs[3]  # Overview, Time, Txns, Drill, Merchants, Subs, Settings
    cat_select = next(sb for sb in drill_tab.selectbox if sb.label == "Select parent category")
    cat_select.set_value("Investments").run()

    drill_tab2 = at.tabs[3]
    assert not at.exception
    assert _has_card(drill_tab2, "Invested", "£300.00")
    assert _has_card(drill_tab2, "Spend", "£0.00")


def test_top_merchants_search_filters_results(tmp_path):
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([
        {"id": "tx_tesco", "payload": {"data": {
            "amount": -1000, "currency": "GBP", "description": "shop",
            "category": "groceries", "merchant": {"name": "Tesco"},
            "counterparty": {"name": ""}, "is_load": False,
            "created": "2026-01-10T10:00:00", "settled": "2026-01-10T10:00:00",
        }}, "user_context": "shop", "skipped": False,
         "received_at": "2026-01-10T10:00:00", "enriched_at": "2026-01-10T10:00:01"},
        {"id": "tx_asda", "payload": {"data": {
            "amount": -2000, "currency": "GBP", "description": "shop",
            "category": "groceries", "merchant": {"name": "Asda"},
            "counterparty": {"name": ""}, "is_load": False,
            "created": "2026-01-11T10:00:00", "settled": "2026-01-11T10:00:00",
        }}, "user_context": "shop", "skipped": False,
         "received_at": "2026-01-11T10:00:00", "enriched_at": "2026-01-11T10:00:01"},
    ])

    at = AppTest.from_file(_DASHBOARD_PATH).run()
    merchants_tab = at.tabs[4]
    search_box = next(ti for ti in merchants_tab.text_input if ti.key == "merchant_search")
    search_box.set_value("Tesco").run()

    merchants_tab2 = at.tabs[4]
    assert not at.exception
    assert any("All Merchants (1)" in s.value for s in merchants_tab2.subheader)


def test_inbound_and_outbound_transfers_match_between_overview_and_drilldown(tmp_path):
    """Regression test for the actual bug reported: Category Drill-Down showed
    £674 for Outbound Transfer (correct, computed per-subcategory), but
    Overview's "Transferred / Excluded" showed only £134 -- summing the
    Outbound (-674) and Inbound (+540) transfers together before taking abs()
    let them net to -134 first. Both should agree on £674 (Outbound only,
    since Overview's figure is now outgoing-only)."""
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([
        {"id": "tx_out", "payload": {"data": {
            "amount": -67400, "currency": "GBP", "description": "Outbound",
            "category": "transfer", "merchant": None, "counterparty": {"name": ""},
            "is_load": False, "created": "2026-01-10T10:00:00", "settled": "2026-01-10T10:00:00",
        }}, "user_context": "transfer out", "skipped": False,
         "received_at": "2026-01-10T10:00:00", "enriched_at": "2026-01-10T10:00:01"},
        {"id": "tx_in", "payload": {"data": {
            "amount": 54000, "currency": "GBP", "description": "Inbound",
            "category": "transfer", "merchant": None, "counterparty": {"name": ""},
            "is_load": False, "created": "2026-01-11T10:00:00", "settled": "2026-01-11T10:00:00",
        }}, "user_context": "transfer in", "skipped": False,
         "received_at": "2026-01-11T10:00:00", "enriched_at": "2026-01-11T10:00:01"},
    ])
    database_functions.update_classification("tx_out", "Transfers", "Outbound Transfer", 1.0, "test")
    database_functions.update_classification("tx_in", "Transfers", "Inbound Transfer", 1.0, "test")

    at = AppTest.from_file(_DASHBOARD_PATH).run()

    drill_tab = at.tabs[3]  # Overview, Time, Txns, Drill, Merchants, Subs, Settings
    cat_select = next(sb for sb in drill_tab.selectbox if sb.label == "Select parent category")
    cat_select.set_value("Transfers").run()
    drill_tab2 = at.tabs[3]
    assert not at.exception
    assert _has_card(drill_tab2, "Transferred / Excluded", "£674.00")

    overview_tab = at.tabs[0]
    assert _has_card(overview_tab, "Transferred / Excluded", "£674.00")


def _seed_two_merchants(tmp_path):
    """Two spend transactions with distinct payees -- one reporting a
    merchant_name, one reporting only a counterparty_name (the direct-debit
    shape), so the exclude filter is exercised against both sides of
    merchant_display_name()'s fallback."""
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([
        {"id": "tx_tesco", "payload": {"data": {
            "amount": -999, "currency": "GBP", "description": "Weekly shop",
            "category": "groceries", "merchant": {"name": "Tesco"},
            "counterparty": {"name": ""}, "is_load": False,
            "created": "2026-01-15T10:00:00", "settled": "2026-01-15T10:00:00",
        }}, "user_context": "weekly shop", "skipped": False,
         "received_at": "2026-01-15T10:00:00", "enriched_at": "2026-01-15T10:00:01"},
        {"id": "tx_rent", "payload": {"data": {
            "amount": -50000, "currency": "GBP", "description": "DD RENT",
            "category": "bills", "merchant": {"name": ""},
            "counterparty": {"name": "Landlord"}, "is_load": False,
            "created": "2026-01-16T10:00:00", "settled": "2026-01-16T10:00:00",
        }}, "user_context": "monthly rent", "skipped": False,
         "received_at": "2026-01-16T10:00:00", "enriched_at": "2026-01-16T10:00:01"},
    ])


def _txn_count(at) -> str:
    """The Transactions tab's subheader is 'N transactions' for the currently
    filtered set -- the most direct read of what the filters actually did."""
    return at.tabs[2].subheader[0].value


def test_exclude_merchant_offers_counterparty_only_payees(tmp_path):
    """The exclude multiselect must list payees that only ever populate
    counterparty_name (direct debits), not just card merchants."""
    _seed_two_merchants(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    options = at.sidebar.multiselect(key="exclude_merchants").options
    assert "Tesco" in options
    assert "Landlord" in options


def test_exclude_merchant_removes_matching_transactions(tmp_path):
    _seed_two_merchants(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    assert _txn_count(at) == "2 transactions"

    at.sidebar.multiselect(key="exclude_merchants").set_value(["Tesco"]).run()
    assert not at.exception
    assert _txn_count(at) == "1 transactions"


def test_exclude_counterparty_only_payee_removes_its_transactions(tmp_path):
    """Regression guard for the merchant_name='' fallback: excluding a
    direct-debit payee must actually filter it out, not silently match nothing."""
    _seed_two_merchants(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    at.sidebar.multiselect(key="exclude_merchants").set_value(["Landlord"]).run()
    assert not at.exception
    assert _txn_count(at) == "1 transactions"


def test_search_matches_counterparty_only_payees(tmp_path):
    """Regression test: the sidebar search box checked description, user_context
    and merchant_name, but not counterparty_name -- so searching for a direct
    debit's payee (a bill, rent, an HMRC-style payment) found nothing at all,
    even though the same term matches via database_functions.search()."""
    _seed_two_merchants(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    assert _txn_count(at) == "2 transactions"
    at.sidebar.text_input[0].set_value("Landlord").run()
    assert not at.exception
    assert _txn_count(at) == "1 transactions"


def test_biggest_transaction_card_shows_counterparty_not_unknown(tmp_path):
    """Regression test: the "fun facts" cards used raw merchant_name, so the
    £500 rent direct debit (bigger than the £9.99 Tesco card payment) showed
    up as "Unknown" instead of "Landlord" -- and before the NaN-safe rewrite,
    `merchant_display_name()` returning NaN for a missing payee would have
    made `nan or description` skip the description fallback too (NaN is
    truthy in Python), the same trap that broke clearing a transaction's
    category in the Reclassify editor."""
    _seed_two_merchants(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    txns_tab = at.tabs[2]
    assert not at.exception
    assert _has_card(txns_tab, "Biggest Transaction", "Landlord")
    assert _has_card(txns_tab, "Unique Merchants", "2")


def test_exclude_by_id_accepts_newline_and_comma_separated_ids(tmp_path):
    """IDs get pasted in from a CSV or copied one-per-line off the table, so
    both separators (and stray whitespace) have to parse the same way."""
    _seed_two_merchants(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()

    at.sidebar.text_area(key="exclude_ids").set_value("tx_tesco").run()
    assert not at.exception
    assert _txn_count(at) == "1 transactions"

    at.sidebar.text_area(key="exclude_ids").set_value(" tx_tesco , tx_rent ").run()
    assert not at.exception
    assert _txn_count(at) == "0 transactions"

    at.sidebar.text_area(key="exclude_ids").set_value("tx_tesco\n\ntx_rent\n").run()
    assert not at.exception
    assert _txn_count(at) == "0 transactions"


def test_unknown_exclude_id_leaves_data_untouched(tmp_path):
    """A typo'd or stale ID should be a no-op, never an exception."""
    _seed_two_merchants(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    at.sidebar.text_area(key="exclude_ids").set_value("tx_does_not_exist").run()
    assert not at.exception
    assert _txn_count(at) == "2 transactions"


def test_manual_backup_button_writes_a_backup(tmp_path):
    """End-to-end wiring check: the Settings tab's backup button must actually
    produce a restorable export next to the database."""
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    assert database_functions.list_backups() == []

    settings_tab = at.tabs[6]  # Overview, Time, Txns, Drill, Merchants, Subs, Settings
    next(b for b in settings_tab.button if b.label == "Back up now").click().run()

    assert not at.exception
    backups = database_functions.list_backups()
    assert len(backups) == 1
    assert (backups[0] / "schema.sql").is_file()

    # The listing must say what was actually saved, not just that a backup
    # happened -- the seed holds exactly one transaction.
    summary = next(
        md.value for md in at.tabs[6].markdown if "Latest backup" in md.value
    )
    assert "1 transactions" in summary


def _settings_button(at, label: str):
    return next(b for b in at.tabs[6].button if b.label == label)


def test_backup_delete_requires_confirmation(tmp_path):
    """Deleting a backup throws away a safety net, so the first click must only
    ask -- matching how the rest of the app handles destructive actions."""
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    _settings_button(at, "Back up now").click().run()
    assert len(database_functions.list_backups()) == 1

    _settings_button(at, "Delete backup").click().run()

    assert not at.exception
    assert len(database_functions.list_backups()) == 1, "must not delete on the first click"
    assert any("cannot be recovered" in w.value for w in at.tabs[6].warning)


def test_backup_delete_confirmed_removes_it(tmp_path):
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    _settings_button(at, "Back up now").click().run()
    _settings_button(at, "Delete backup").click().run()
    _settings_button(at, "Yes, delete it").click().run()

    assert not at.exception
    assert database_functions.list_backups() == []


def test_backup_delete_can_be_cancelled(tmp_path):
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    _settings_button(at, "Back up now").click().run()
    before = database_functions.list_backups()

    _settings_button(at, "Delete backup").click().run()
    next(b for b in at.tabs[6].button if b.key == "confirm_del_backup_no").click().run()
    # AppTest does not follow the st.rerun() the Cancel handler triggers -- the
    # captured tree is the one rendered up to that call, which still holds the
    # confirmation. One more run() gives the tree the browser would show.
    at.run()

    assert not at.exception
    assert database_functions.list_backups() == before
    assert not any("cannot be recovered" in w.value for w in at.tabs[6].warning)


def test_backup_delete_warns_when_it_is_the_only_one(tmp_path):
    _seed(tmp_path)
    at = AppTest.from_file(_DASHBOARD_PATH).run()
    _settings_button(at, "Back up now").click().run()
    _settings_button(at, "Delete backup").click().run()

    warning = next(w.value for w in at.tabs[6].warning if "cannot be recovered" in w.value)
    assert "only backup" in warning


def _seed_date_spread(tmp_path):
    """Four transactions spread across the year, so a custom date range can
    actually narrow the result set instead of just returning everything."""
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([
        {"id": f"tx_{i:03}", "payload": {"data": {
            "amount": -100 - i, "currency": "GBP", "description": f"Txn {i}",
            "category": "groceries", "merchant": {"name": "Tesco"},
            "counterparty": {"name": ""}, "is_load": False,
            "created": f"{day}T10:00:00", "settled": f"{day}T10:00:00",
        }}, "user_context": f"txn {i}", "skipped": False,
         "received_at": f"{day}T10:00:00", "enriched_at": f"{day}T10:00:01"}
        for i, day in enumerate(["2026-01-05", "2026-03-10", "2026-06-20", "2026-09-25"])
    ])


class TestDateRangeFilter:
    """Regression tests for the combined st.date_input range picker, which could
    visually close or reset after the first click before a second date was
    chosen -- looking exactly like the date filter breaking. Replaced with two
    independent date_input boxes, each of which only ever holds one date."""

    def test_two_separate_clicks_narrow_the_range(self, tmp_path):
        """The real sequence a user hits: set the start box, rerun (as
        Streamlit does after every widget interaction), then set the end box --
        never a single combined tuple."""
        _seed_date_spread(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        assert _txn_count(at) == "4 transactions"

        at.sidebar.date_input(key="date_from").set_value(date(2026, 2, 1)).run()
        assert not at.exception
        at.sidebar.date_input(key="date_to").set_value(date(2026, 8, 1)).run()
        assert not at.exception
        assert _txn_count(at) == "2 transactions"

    def test_start_and_end_are_sorted_regardless_of_which_box_holds_which(self, tmp_path):
        """The combined widget used to enforce start <= end itself; two
        independent boxes don't, so the app must sort them rather than let a
        reversed pick silently return nothing (or everything)."""
        _seed_date_spread(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()

        at.sidebar.date_input(key="date_from").set_value(date(2026, 8, 1)).run()
        at.sidebar.date_input(key="date_to").set_value(date(2026, 2, 1)).run()

        assert not at.exception
        assert _txn_count(at) == "2 transactions"

    def test_changing_the_preset_updates_both_boxes(self, tmp_path):
        """"This month" is computed from the real date.today(), not the seed
        data's fixed range, so its exact value depends on when this test
        happens to run -- but it must always end up clamped into
        [min_date, max_date] rather than crashing (regression: date_input
        rejects a default value outside its own min/max, which "This month"
        hits every time the real calendar rolls past the latest transaction
        actually on record -- i.e. the start of every month, until that
        month's first transaction lands)."""
        _seed_date_spread(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        at.sidebar.date_input(key="date_from").set_value(date(2026, 2, 1)).run()
        at.sidebar.date_input(key="date_to").set_value(date(2026, 8, 1)).run()
        assert _txn_count(at) == "2 transactions"

        at.sidebar.radio(key="date_preset").set_value("This month").run()

        assert not at.exception
        min_date, max_date = date(2026, 1, 5), date(2026, 9, 25)  # _seed_date_spread's range
        expected_from = min(max(date.today().replace(day=1), min_date), max_date)
        assert at.sidebar.date_input(key="date_from").value == expected_from
        assert min_date <= at.sidebar.date_input(key="date_to").value <= max_date

    def test_reset_button_reverts_manual_edits_to_the_current_preset(self, tmp_path):
        _seed_date_spread(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        at.sidebar.date_input(key="date_from").set_value(date(2026, 2, 1)).run()
        at.sidebar.date_input(key="date_to").set_value(date(2026, 8, 1)).run()
        assert _txn_count(at) == "2 transactions"

        reset_btn = next(b for b in at.sidebar.button if b.label == "Reset date range")
        reset_btn.click().run()

        assert not at.exception
        assert _txn_count(at) == "4 transactions"


def _seed_investment_and_saving(tmp_path):
    """One real investment contribution and one savings-pot deposit, same
    month, same magnitude-adjacent but distinct amounts so a sum-together bug
    is unmistakable (£250 + £500 = £750, nothing in the real figures)."""
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([
        {"id": "tx_invest", "payload": {"data": {
            "amount": -25000, "currency": "GBP", "description": "212TRW2R2212T",
            "category": "general", "merchant": None, "counterparty": {"name": "Trading 212"},
            "is_load": False, "created": "2026-10-19T12:13:26", "settled": "2026-10-19T12:13:26",
        }}, "user_context": "Invested into HSBC MSCI world", "skipped": False,
         "received_at": "2026-10-19T12:13:27", "enriched_at": "2026-10-19T12:14:32"},
        {"id": "tx_save", "payload": {"data": {
            "amount": -50000, "currency": "GBP", "description": "pot_0000B7cenLixHWSDlwwxKD",
            "category": "general", "merchant": None, "counterparty": {}, "scheme": "uk_retail_pot",
            "is_load": False, "created": "2026-10-15T09:44:43", "settled": "2026-10-15T09:44:43",
        }}, "user_context": "Moved into savings pot", "skipped": False,
         "received_at": "2026-10-15T09:44:44", "enriched_at": "2026-10-15T09:45:00"},
    ])
    invest_parent = database_functions.upsert_parent("Investments")
    invest_sub = database_functions.upsert_subcategory("General Investment Account Contributions", invest_parent)
    database_functions.set_parent_role(invest_parent, "investment")
    save_parent = database_functions.upsert_parent("Savings")
    save_sub = database_functions.upsert_subcategory("Deposit", save_parent)
    database_functions.set_parent_role(save_parent, "saving")
    database_functions.update_classification("tx_invest", "Investments", "General Investment Account Contributions", None, "test")
    database_functions.update_classification("tx_save", "Savings", "Deposit", None, "test")


class TestInvestedAndSavedStayNumericallySeparate:
    """The exact regression this role exists to prevent: a real investment
    contribution and a savings-pot deposit must never be combined under one
    figure, even though they're mechanically identical (both excluded from
    Actual Spend/Income, both tracked as their own outgoing-only total)."""

    def test_overview_cards_show_each_figure_separately_not_summed(self, tmp_path):
        _seed_investment_and_saving(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        assert not at.exception
        overview = at.tabs[0]
        assert _has_card(overview, "Invested", "£250.00")
        assert _has_card(overview, "Saved", "£500.00")
        assert not _has_card(overview, "Invested", "£750.00")
        assert not _has_card(overview, "Saved", "£750.00")

    def test_actual_spend_and_net_are_unaffected_by_either(self, tmp_path):
        """Both roles are excluded from Actual Spend/Income/Net entirely --
        neither a contribution nor a pot deposit is real spend."""
        _seed_investment_and_saving(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        overview = at.tabs[0]
        assert _has_card(overview, "Spend", "£0.00")
        assert _has_card(overview, "Net", "£0.00")

    def test_category_drill_down_keeps_them_on_separate_cards(self, tmp_path):
        _seed_investment_and_saving(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        drill = at.tabs[3]  # Overview, Time, Txns, Drill, Merchants, Subs, Settings
        drill.selectbox(key="drill_selected_category").set_value("Investments").run()
        assert _has_card(at.tabs[3], "Invested", "£250.00")
        assert not _has_card(at.tabs[3], "Saved", "£500.00"), \
            "the Investments drill-down must not show the Savings figure at all"

        drill.selectbox(key="drill_selected_category").set_value("Savings").run()
        assert _has_card(at.tabs[3], "Saved", "£500.00")
        assert not _has_card(at.tabs[3], "Invested", "£250.00"), \
            "the Savings drill-down must not show the Investments figure at all"

    def test_the_outgoing_breakdown_dropdown_offers_saved_as_its_own_metric(self, tmp_path):
        _seed_investment_and_saving(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        options = at.tabs[0].selectbox(key="overview_outgoing_metric").options
        assert "Saved" in options and "Invested" in options

    def test_monthly_breakdown_table_has_both_as_separate_columns(self, tmp_path):
        _seed_investment_and_saving(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        df = at.tabs[0].dataframe[0].value
        assert "Invested (£)" in df.columns and "Saved (£)" in df.columns
        row = df.iloc[0]
        assert row["Invested (£)"] == "£250.00" and row["Saved (£)"] == "£500.00"

    def test_standard_row_omits_the_roles_it_never_populates(self, tmp_path):
        """Invested/Saved/Transferred have no Standard-side value at all (that
        money is just folded into Standard Spend by amount sign), so the
        Standard row shows only Income/Spend/Net/Savings Rate -- it must not
        render a dead card stuck permanently on '—'."""
        _seed_investment_and_saving(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        overview = at.tabs[0]
        assert not _has_card(overview, "Invested", "—")
        assert not _has_card(overview, "Saved", "—")
        assert not _has_card(overview, "Transferred / Excluded", "—")
        assert _has_card(overview, "Invested", "£250.00")
        assert _has_card(overview, "Saved", "£500.00")


def _seed_saving_deposit_and_withdrawal(tmp_path):
    """A £500 pot deposit and a later £200 withdrawal, same role -- the same
    pot cycling, so Saved should report the net £300.00, not vanish or split
    into separate Outgoing/Incoming cards."""
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([
        {"id": "tx_deposit", "payload": {"data": {
            "amount": -50000, "currency": "GBP", "description": "pot_0000B7cenLixHWSDlwwxKD",
            "category": "general", "merchant": None, "counterparty": {}, "scheme": "uk_retail_pot",
            "is_load": False, "created": "2026-10-15T09:44:43", "settled": "2026-10-15T09:44:43",
        }}, "user_context": "Moved into savings pot", "skipped": False,
         "received_at": "2026-10-15T09:44:44", "enriched_at": "2026-10-15T09:45:00"},
        {"id": "tx_withdrawal", "payload": {"data": {
            "amount": 20000, "currency": "GBP", "description": "pot_0000B7cenLixHWSDlwwxKD",
            "category": "general", "merchant": None, "counterparty": {}, "scheme": "uk_retail_pot",
            "is_load": False, "created": "2026-10-20T09:44:43", "settled": "2026-10-20T09:44:43",
        }}, "user_context": "Moved out of savings pot", "skipped": False,
         "received_at": "2026-10-20T09:44:44", "enriched_at": "2026-10-20T09:45:00"},
    ])
    save_parent = database_functions.upsert_parent("Savings")
    deposit_sub = database_functions.upsert_subcategory("Deposit", save_parent)
    withdrawal_sub = database_functions.upsert_subcategory("Withdrawal", save_parent)
    database_functions.set_parent_role(save_parent, "saving")
    database_functions.update_classification("tx_deposit", "Savings", "Deposit", None, "test")
    database_functions.update_classification("tx_withdrawal", "Savings", "Withdrawal", None, "test")


def _seed_transfer_in_and_out(tmp_path):
    """An unrelated £300 inbound transfer and £120 outbound transfer, same
    role -- these are NOT the same pot cycling (could be a friend repaying
    you and an unrelated bill payment), so Transferred/Excluded must keep
    showing them as separate Outgoing/Incoming cards rather than netting."""
    database_functions.DB_PATH = str(tmp_path / "test_dashboard.db")
    database_functions.init_db()
    database_functions.write_to_db([
        {"id": "tx_transfer_in", "payload": {"data": {
            "amount": 30000, "currency": "GBP", "description": "FROM A FRIEND",
            "category": "general", "merchant": None, "counterparty": {"name": "A Friend"},
            "is_load": False, "created": "2026-10-15T09:44:43", "settled": "2026-10-15T09:44:43",
        }}, "user_context": "Friend repaid me", "skipped": False,
         "received_at": "2026-10-15T09:44:44", "enriched_at": "2026-10-15T09:45:00"},
        {"id": "tx_transfer_out", "payload": {"data": {
            "amount": -12000, "currency": "GBP", "description": "TO LANDLORD",
            "category": "general", "merchant": None, "counterparty": {"name": "Landlord"},
            "is_load": False, "created": "2026-10-16T09:44:43", "settled": "2026-10-16T09:44:43",
        }}, "user_context": "Rent transfer", "skipped": False,
         "received_at": "2026-10-16T09:44:44", "enriched_at": "2026-10-16T09:45:00"},
    ])
    parent = database_functions.upsert_parent("Transfers")
    database_functions.upsert_subcategory("Inbound Transfer", parent)
    database_functions.upsert_subcategory("Outbound Transfer", parent)
    database_functions.set_parent_role(parent, "transfer")
    database_functions.update_classification("tx_transfer_in", "Transfers", "Inbound Transfer", None, "test")
    database_functions.update_classification("tx_transfer_out", "Transfers", "Outbound Transfer", None, "test")


class TestOverviewNetsSavingAndInvestmentButNotTransfers:
    """Saved/Invested net their two directions together into one card (a
    deposit and a later withdrawal are the same pot or account). Transferred/
    Excluded stays split into separate Outgoing/Incoming cards, since that
    bucket can mix genuinely unrelated inbound and outbound movements that
    would net into a misleading number."""

    def test_saved_nets_deposit_and_withdrawal_into_one_card(self, tmp_path):
        _seed_saving_deposit_and_withdrawal(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        overview = at.tabs[0]
        assert _has_card(overview, "Saved", "£300.00")
        assert not _has_card(overview, "Saved (Outgoing)", "£500.00")
        assert not _has_card(overview, "Saved (Incoming)", "£200.00")

    def test_transferred_excluded_still_splits_into_outgoing_and_incoming(self, tmp_path):
        _seed_transfer_in_and_out(tmp_path)
        at = AppTest.from_file(_DASHBOARD_PATH).run()
        overview = at.tabs[0]
        assert _has_card(overview, "Transferred / Excluded (Outgoing)", "£120.00")
        assert _has_card(overview, "Transferred / Excluded (Incoming)", "£300.00")
        assert not _has_card(overview, "Transferred / Excluded", "£180.00"), \
            "unrelated inbound/outbound transfers must not net into a single figure"
