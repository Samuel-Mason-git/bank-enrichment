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
