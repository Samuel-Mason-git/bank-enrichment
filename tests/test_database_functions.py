import duckdb
import pytest

import database_functions
from database_functions import (
    get_con, write_to_db, get_transaction, get_all_transactions,
    get_unclassified, get_recent, get_stats, update_classification,
    upsert_parent, upsert_subcategory, get_parents, get_subcategories,
    get_subscriptions, upsert_subscription, toggle_subscription,
    delete_subscription, search, _ts, _rows,
    get_top_subcategories, get_top_merchant_subcategories,
    apply_quick_tap_classifications,
    get_totals_by_role, get_category_totals_by_role, set_parent_role, set_subcategory_role,
    _seed_taxonomy,
)


def _txn(id="tx_001", amount_pence=-1234, description="Coffee",
         user_context="morning coffee", skipped=False, merchant="Caffe Nero",
         created="2026-01-15T10:00:00"):
    return {
        "id": id,
        "payload": {
            "data": {
                "amount": amount_pence,
                "currency": "GBP",
                "description": description,
                "category": "eating_out",
                "merchant": {"name": merchant, "category": "coffee"},
                "counterparty": {"name": ""},
                "is_load": False,
                "created": created,
                "settled": created,
            }
        },
        "user_context": user_context,
        "skipped": skipped,
        "received_at": "2026-01-15T10:00:00",
        "enriched_at": "2026-01-15T10:00:01",
    }


class TestGetCon:
    def test_raises_before_init(self):
        with pytest.raises(RuntimeError, match="not initialised"):
            get_con()

    def test_returns_connection_after_db_fixture(self, db):
        assert get_con() is db


class TestWriteAndRead:
    def test_write_and_get_transaction(self, db):
        write_to_db([_txn()])
        t = get_transaction("tx_001")
        assert t is not None
        assert t["id"] == "tx_001"
        assert float(t["amount"]) == pytest.approx(-12.34)
        assert t["merchant_name"] == "Caffe Nero"
        assert t["user_context"] == "morning coffee"

    def test_amount_converted_from_pence(self, db):
        write_to_db([_txn(amount_pence=500)])
        assert float(get_transaction("tx_001")["amount"]) == pytest.approx(5.00)

    def test_get_transaction_not_found(self, db):
        assert get_transaction("nonexistent") is None

    def test_duplicate_write_is_ignored(self, db):
        write_to_db([_txn()])
        write_to_db([_txn()])
        assert len(get_all_transactions()) == 1

    def test_get_all_transactions_returns_all(self, db):
        write_to_db([_txn("tx_001"), _txn("tx_002")])
        assert len(get_all_transactions()) == 2

    def test_get_recent_limits_results(self, db):
        write_to_db([_txn(f"tx_{i:03d}") for i in range(5)])
        assert len(get_recent(3)) == 3

    def test_get_recent_default_is_ten(self, db):
        write_to_db([_txn(f"tx_{i:03d}") for i in range(15)])
        assert len(get_recent()) == 10

    def test_skipped_flag_stored(self, db):
        write_to_db([_txn(skipped=True)])
        assert get_transaction("tx_001")["skipped"] is True


class TestGetUnclassified:
    def test_unclassified_transaction_returned(self, db):
        write_to_db([_txn()])
        assert len(get_unclassified()) == 1

    def test_classified_transaction_excluded(self, db):
        write_to_db([_txn()])
        update_classification("tx_001", "Food & Drink", "Coffee Shops", 0.9, "test-model")
        assert get_unclassified() == []

    def test_skipped_transaction_excluded(self, db):
        write_to_db([_txn(skipped=True)])
        assert get_unclassified() == []

    def test_mix_of_states(self, db):
        write_to_db([_txn("tx_001"), _txn("tx_002", skipped=True), _txn("tx_003")])
        update_classification("tx_003", "Food & Drink", "Coffee", None, "m")
        assert len(get_unclassified()) == 1
        assert get_unclassified()[0]["id"] == "tx_001"


class TestGetStats:
    def test_empty_db(self, db):
        s = get_stats()
        assert s["total"] == 0
        assert s["total_spend"] == 0
        assert s["total_income"] == 0
        assert s["unclassified"] == 0

    def test_spend_and_income_totals(self, db):
        write_to_db([
            _txn("tx_001", amount_pence=-1000),
            _txn("tx_002", amount_pence=5000),
        ])
        s = get_stats()
        assert s["total"] == 2
        assert float(s["total_spend"]) == pytest.approx(10.0)
        assert float(s["total_income"]) == pytest.approx(50.0)
        assert s["unclassified"] == 2

    def test_skipped_not_counted_as_unclassified(self, db):
        write_to_db([_txn(skipped=True)])
        assert get_stats()["unclassified"] == 0

    def test_by_category_after_classification(self, db):
        write_to_db([_txn("tx_001", amount_pence=-500)])
        update_classification("tx_001", "Food & Drink", "Coffee", None, "test")
        cats = get_stats()["by_category"]
        assert len(cats) == 1
        assert cats[0]["category"] == "Food & Drink"
        assert cats[0]["count"] == 1


class TestUpdateClassification:
    def test_sets_all_classification_fields(self, db):
        write_to_db([_txn()])
        update_classification("tx_001", "Food & Drink", "Coffee Shops", 0.95, "claude-test")
        t = get_transaction("tx_001")
        assert t["llm_category"] == "Food & Drink"
        assert t["llm_subcategory"] == "Coffee Shops"
        assert float(t["llm_confidence"]) == pytest.approx(0.95)
        assert t["llm_model"] == "claude-test"
        assert t["classified_at"] is not None

    def test_allows_null_subcategory(self, db):
        write_to_db([_txn()])
        update_classification("tx_001", "Income", None, None, "test")
        t = get_transaction("tx_001")
        assert t["llm_category"] == "Income"
        assert t["llm_subcategory"] is None


class TestUpsertParent:
    def test_creates_parent(self, db):
        pid = upsert_parent("Transport")
        assert isinstance(pid, int)
        names = [p["name"] for p in get_parents()]
        assert "Transport" in names

    def test_returns_same_id_on_duplicate(self, db):
        id1 = upsert_parent("Transport")
        id2 = upsert_parent("Transport")
        assert id1 == id2

    def test_multiple_parents_get_distinct_ids(self, db):
        id1 = upsert_parent("Transport")
        id2 = upsert_parent("Food & Drink")
        assert id1 != id2


class TestUpsertSubcategory:
    def test_creates_subcategory_under_parent(self, db):
        pid = upsert_parent("Transport")
        sid = upsert_subcategory("Fuel", pid)
        assert isinstance(sid, int)
        names = [s["name"] for s in get_subcategories()]
        assert "Fuel" in names

    def test_returns_same_id_on_duplicate(self, db):
        pid = upsert_parent("Transport")
        id1 = upsert_subcategory("Fuel", pid)
        id2 = upsert_subcategory("Fuel", pid)
        assert id1 == id2

    def test_same_name_different_parent_is_separate(self, db):
        p1 = upsert_parent("Transport")
        p2 = upsert_parent("Holidays")
        s1 = upsert_subcategory("Taxi", p1)
        s2 = upsert_subcategory("Taxi", p2)
        assert s1 != s2


class TestSeedTaxonomy:
    def test_seeds_full_default_taxonomy_on_empty_db(self, db):
        _seed_taxonomy()
        names = {p["name"] for p in get_parents()}
        assert "Income" in names
        assert "Food & Drink" in names

    def test_does_not_top_up_a_custom_taxonomy(self, db):
        """Regression test: seeding used to run on every startup and insert
        any default category not already present by name -- so a user who
        wiped the taxonomy and built their own under different names would
        find the entire default taxonomy silently added back in alongside
        it on the next start. Seeding must now be skipped entirely once any
        parent category exists, custom or otherwise."""
        upsert_parent("My Custom Category")
        _seed_taxonomy()
        names = {p["name"] for p in get_parents()}
        assert names == {"My Custom Category"}

    def test_does_not_top_up_missing_subcategories_of_an_existing_default_parent(self, db):
        """Even a parent that happens to share a default name shouldn't have
        its missing default subcategories filled back in once anything
        exists in the table -- seeding is all-or-nothing on an empty table,
        not a per-category top-up."""
        pid = upsert_parent("Income")
        upsert_subcategory("Salary", pid)
        _seed_taxonomy()
        sub_names = {s["name"] for s in get_subcategories() if s["parent_name"] == "Income"}
        assert sub_names == {"Salary"}


class TestRoles:
    def test_parent_defaults_to_spend_when_no_override(self, db):
        upsert_parent("Bills & Utilities")
        assert get_parents()[0]["role"] == "spend"

    def test_income_investments_transfers_have_hardcoded_defaults(self, db):
        upsert_parent("Income")
        upsert_parent("Investments")
        upsert_parent("Transfers")
        roles = {p["name"]: p["role"] for p in get_parents()}
        assert roles["Income"] == "income"
        assert roles["Investments"] == "investment"
        assert roles["Transfers"] == "transfer"

    def test_set_parent_role_overrides_default(self, db):
        pid = upsert_parent("Bills & Utilities")
        set_parent_role(pid, "excluded")
        assert get_parents()[0]["role"] == "excluded"

    def test_set_parent_role_rejects_invalid_role(self, db):
        pid = upsert_parent("Bills & Utilities")
        with pytest.raises(ValueError):
            set_parent_role(pid, "not_a_role")

    def test_subcategory_inherits_parent_role_by_default(self, db):
        pid = upsert_parent("Income")
        sid = upsert_subcategory("Salary", pid)
        sub = next(s for s in get_subcategories() if s["id"] == sid)
        assert sub["role_override"] is None
        assert sub["parent_role"] == "income"

    def test_set_subcategory_role_overrides_parent(self, db):
        pid = upsert_parent("Income")
        sid = upsert_subcategory("Refunds", pid)
        set_subcategory_role(sid, "excluded")
        sub = next(s for s in get_subcategories() if s["id"] == sid)
        assert sub["role_override"] == "excluded"

    def test_clearing_subcategory_override_falls_back_to_inherit(self, db):
        pid = upsert_parent("Income")
        sid = upsert_subcategory("Refunds", pid)
        set_subcategory_role(sid, "excluded")
        set_subcategory_role(sid, None)
        sub = next(s for s in get_subcategories() if s["id"] == sid)
        assert sub["role_override"] is None

    def test_set_subcategory_role_rejects_invalid_role(self, db):
        pid = upsert_parent("Income")
        sid = upsert_subcategory("Salary", pid)
        with pytest.raises(ValueError):
            set_subcategory_role(sid, "not_a_role")


class TestGetTotalsByRole:
    def test_refund_does_not_count_as_income_or_outgoing(self, db):
        """Refunds are positive (incoming), so under the outgoing-only
        definition of every non-income role, a Refund contributes to neither
        "income" (wrong role) nor "excluded" (wrong direction) -- it's simply
        not part of either headline figure, which is correct: it was never
        new income, and it's not money leaving either."""
        _seed_taxonomy()  # real startup path: seeds Income -> Refunds -> excluded
        write_to_db([
            _txn(id="t1", amount_pence=5000, description="Refund", created="2026-01-05T10:00:00"),
            _txn(id="t2", amount_pence=200000, description="Salary", created="2026-01-06T10:00:00"),
        ])
        update_classification("t1", "Income", "Refunds", 1.0, "test")
        update_classification("t2", "Income", "Salary", 1.0, "test")

        totals = get_totals_by_role("2026-01-01", "2026-01-31")
        assert totals["income"] == pytest.approx(2000.0)
        assert totals["excluded"] == pytest.approx(0.0)

    def test_investment_reported_as_positive_outgoing_magnitude(self, db):
        pid = upsert_parent("Investments")
        upsert_subcategory("Stocks & Shares ISA Contributions", pid)
        write_to_db([_txn(id="t1", amount_pence=-30000, description="ISA", created="2026-01-05T10:00:00")])
        update_classification("t1", "Investments", "Stocks & Shares ISA Contributions", 1.0, "test")

        totals = get_totals_by_role("2026-01-01", "2026-01-31")
        assert totals["investment"] == pytest.approx(300.0)
        assert totals["spend"] == pytest.approx(0.0)

    def test_inbound_and_outbound_transfers_do_not_cancel_out(self, db):
        """Regression test for the actual bug reported: a "Transfers" category
        has both an Inbound (positive) and Outbound (negative) subcategory
        sharing the "transfer" role. Summing signed amounts before reporting
        used to let a £540 Inbound Transfer cancel most of a £674 Outbound
        Transfer, reporting a misleading £134 instead of the true £674 that
        actually left the account."""
        pid = upsert_parent("Transfers")
        upsert_subcategory("Inbound Transfer", pid)
        upsert_subcategory("Outbound Transfer", pid)
        write_to_db([
            _txn(id="t1", amount_pence=-67400, description="Outbound", created="2026-01-05T10:00:00"),
            _txn(id="t2", amount_pence=54000, description="Inbound", created="2026-01-06T10:00:00"),
        ])
        update_classification("t1", "Transfers", "Outbound Transfer", 1.0, "test")
        update_classification("t2", "Transfers", "Inbound Transfer", 1.0, "test")

        totals = get_totals_by_role("2026-01-01", "2026-01-31")
        assert totals["transfer"] == pytest.approx(674.0)

    def test_all_five_roles_always_present(self, db):
        totals = get_totals_by_role("2026-01-01", "2026-01-31")
        assert set(totals.keys()) == {"income", "spend", "investment", "transfer", "excluded"}

    def test_positive_amount_under_ordinary_category_is_not_outgoing_or_income(self, db):
        """Regression test: a real category with no role set at all (not one
        of Income/Investments/Transfers) used to fall all the way through to
        the amount-sign fallback meant only for genuinely unclassified
        transactions -- so e.g. a positive-amount refund transaction under
        Holidays got silently counted as income. It now correctly resolves to
        role='spend' (Holidays' default), and since spend is outgoing-only,
        a positive amount there contributes to neither spend nor income."""
        pid = upsert_parent("Holidays")
        upsert_subcategory("Accommodation", pid)
        write_to_db([_txn(id="t1", amount_pence=5000, description="Refunded hotel", created="2026-01-05T10:00:00")])
        update_classification("t1", "Holidays", "Accommodation", 1.0, "test")

        totals = get_totals_by_role("2026-01-01", "2026-01-31")
        assert totals["spend"] == pytest.approx(0.0)
        assert totals["income"] == pytest.approx(0.0)

    def test_rejects_invalid_role(self, db):
        with pytest.raises(ValueError):
            get_category_totals_by_role("not_a_role", "2026-01-01", "2026-01-31")


class TestSubscriptions:
    def test_upsert_and_get(self, db):
        upsert_subscription("Netflix", 10.99, "monthly", "Netflix")
        subs = get_subscriptions()
        assert len(subs) == 1
        assert subs[0]["name"] == "Netflix"
        assert float(subs[0]["amount"]) == pytest.approx(10.99)
        assert subs[0]["active"] is True

    def test_toggle_deactivates_then_reactivates(self, db):
        sub_id = upsert_subscription("Netflix", 10.99, "monthly")
        toggle_subscription(sub_id)
        assert get_subscriptions()[0]["active"] is False
        toggle_subscription(sub_id)
        assert get_subscriptions()[0]["active"] is True

    def test_delete_removes_subscription(self, db):
        sub_id = upsert_subscription("Netflix", 10.99, "monthly")
        delete_subscription(sub_id)
        assert get_subscriptions() == []

    def test_multiple_subs_ordered_active_first(self, db):
        id1 = upsert_subscription("Active", 5.0, "monthly")
        id2 = upsert_subscription("Inactive", 3.0, "monthly")
        toggle_subscription(id2)
        subs = get_subscriptions()
        assert subs[0]["active"] is True
        assert subs[1]["active"] is False


class TestSearch:
    def test_finds_by_description(self, db):
        write_to_db([_txn(description="Costa Coffee")])
        assert len(search("Costa")) == 1

    def test_finds_by_merchant_name(self, db):
        write_to_db([_txn(merchant="Caffe Nero")])
        assert len(search("Nero")) == 1

    def test_finds_by_user_context(self, db):
        write_to_db([_txn(user_context="holiday dinner")])
        assert len(search("holiday")) == 1

    def test_case_insensitive(self, db):
        write_to_db([_txn(description="Starbucks")])
        assert len(search("starbucks")) == 1

    def test_no_match_returns_empty(self, db):
        write_to_db([_txn(description="Costa Coffee")])
        assert search("Tesco") == []

    def test_does_not_return_duplicates_for_multi_field_match(self, db):
        write_to_db([_txn(description="Tesco", merchant="Tesco")])
        results = search("Tesco")
        ids = [r["id"] for r in results]
        assert len(ids) == len(set(ids))


def _seed(pairs):
    """upsert_parent/upsert_subcategory for each (category, subcategory) pair."""
    for category, subcategory in pairs:
        pid = upsert_parent(category)
        upsert_subcategory(subcategory, pid)


class TestGetTopSubcategories:
    def test_orders_by_frequency(self, db):
        write_to_db([_txn("tx_001"), _txn("tx_002"), _txn("tx_003")])
        _seed([("Food & Drink", "Coffee Shops"), ("Transport", "Fuel")])
        update_classification("tx_001", "Food & Drink", "Coffee Shops", None, "m")
        update_classification("tx_002", "Food & Drink", "Coffee Shops", None, "m")
        update_classification("tx_003", "Transport", "Fuel", None, "m")
        rows = get_top_subcategories()
        assert rows[0]["subcategory"] == "Coffee Shops"
        assert rows[0]["transaction_count"] == 2

    def test_limit_respected(self, db):
        write_to_db([_txn(f"tx_{i:03d}") for i in range(4)])
        pairs = [("Food & Drink", "Coffee Shops"), ("Food & Drink", "Groceries"),
                  ("Transport", "Fuel"), ("Transport", "Parking")]
        _seed(pairs)
        for i, (cat, sub) in enumerate(pairs):
            update_classification(f"tx_{i:03d}", cat, sub, None, "m")
        assert len(get_top_subcategories(limit=2)) == 2

    def test_unclassified_transactions_excluded(self, db):
        write_to_db([_txn()])
        assert get_top_subcategories() == []

    def test_empty_when_no_transactions(self, db):
        assert get_top_subcategories() == []


class TestGetTopMerchantSubcategories:
    def test_top_subcategories_per_merchant(self, db):
        write_to_db([_txn(f"tx_{i:03d}", merchant="Tesco") for i in range(4)])
        update_classification("tx_000", "Food & Drink", "Groceries", None, "m")
        update_classification("tx_001", "Food & Drink", "Groceries", None, "m")
        update_classification("tx_002", "Food & Drink", "Snacks", None, "m")
        update_classification("tx_003", "Food & Drink", "Takeaway", None, "m")
        rows = get_top_merchant_subcategories(per_merchant_limit=2)
        assert len(rows) == 2
        assert rows[0]["subcategory"] == "Groceries"
        assert rows[0]["merchant_name"] == "Tesco"

    def test_merchant_limit_caps_number_of_merchants(self, db):
        write_to_db([
            _txn("tx_001", merchant="Tesco"), _txn("tx_002", merchant="Tesco"),
            _txn("tx_003", merchant="Amazon"),
        ])
        update_classification("tx_001", "Food & Drink", "Groceries", None, "m")
        update_classification("tx_002", "Food & Drink", "Groceries", None, "m")
        update_classification("tx_003", "Shopping", "Electronics", None, "m")
        rows = get_top_merchant_subcategories(merchant_limit=1, per_merchant_limit=3)
        merchants = {r["merchant_name"] for r in rows}
        assert merchants == {"Tesco"}

    def test_ignores_null_merchant(self, db):
        write_to_db([_txn("tx_001", merchant=None)])
        update_classification("tx_001", "Food & Drink", "Groceries", None, "m")
        assert get_top_merchant_subcategories() == []

    def test_empty_when_no_classified_transactions(self, db):
        write_to_db([_txn(merchant="Tesco")])
        assert get_top_merchant_subcategories() == []

    def _counterparty_txn(self, id="tx_dd", counterparty="Octopus Energy"):
        """A direct debit: no merchant block at all, only a counterparty --
        the shape _txn() can't produce since it always sends merchant."""
        return {
            "id": id,
            "payload": {"data": {
                "amount": -8000, "currency": "GBP", "description": "89GJTS7",
                "category": "bills", "counterparty": {"name": counterparty},
                "is_load": False, "created": "2026-01-15T10:00:00", "settled": "2026-01-15T10:00:00",
            }},
            "user_context": "Electricity bill", "skipped": False,
            "received_at": "2026-01-15T10:00:00", "enriched_at": "2026-01-15T10:00:01",
        }

    def test_falls_back_to_counterparty_when_merchant_is_absent(self, db):
        """Regression test: direct debits and bank transfers -- rent, utility
        bills, HMRC-style payments -- almost never populate merchant_name, only
        counterparty_name. Grouping by merchant_name alone shut every one of
        these recurring payments out of quick-tap suggestions entirely."""
        write_to_db([self._counterparty_txn(f"tx_{i}") for i in range(3)])
        for i in range(3):
            update_classification(f"tx_{i}", "Bills & Utilities", "Electricity", None, "m")
        rows = get_top_merchant_subcategories()
        assert len(rows) == 1
        assert rows[0]["merchant_name"] == "Octopus Energy"
        assert rows[0]["subcategory"] == "Electricity"
        assert rows[0]["transaction_count"] == 3

    def test_merchant_name_is_preferred_over_counterparty_when_both_exist(self, db):
        write_to_db([_txn("tx_card", merchant="Tesco")])
        update_classification("tx_card", "Food & Drink", "Groceries", None, "m")
        rows = get_top_merchant_subcategories()
        assert rows[0]["merchant_name"] == "Tesco"

    def test_a_transaction_with_neither_merchant_nor_counterparty_is_ignored(self, db):
        """counterparty_name is stored as '' rather than NULL when Monzo sends
        no counterparty block -- an empty string must not itself count as a
        real payee identity."""
        write_to_db([_txn("tx_001", merchant=None)])
        update_classification("tx_001", "Food & Drink", "Groceries", None, "m")
        assert get_top_merchant_subcategories() == []

    def test_a_card_payment_and_a_direct_debit_with_the_same_name_pool_together(self, db):
        """The point of falling back to counterparty_name at all: a card swipe
        at 'Tesco' and a direct debit whose counterparty is also 'Tesco' are
        the same real-world payee, so they should count as one merchant with
        two transactions, not be split across a merchant-only and a
        counterparty-only bucket that never combine."""
        write_to_db([_txn("tx_card", merchant="Tesco"), self._counterparty_txn("tx_dd", counterparty="Tesco")])
        update_classification("tx_card", "Food & Drink", "Groceries", None, "m")
        update_classification("tx_dd", "Food & Drink", "Groceries", None, "m")
        rows = get_top_merchant_subcategories()
        assert len(rows) == 1
        assert rows[0]["merchant_name"] == "Tesco"
        assert rows[0]["transaction_count"] == 2


class TestApplyQuickTapClassifications:
    def test_classifies_exact_match(self, db):
        pid = upsert_parent("Food & Drink")
        upsert_subcategory("Coffee Shops", pid)
        write_to_db([_txn(user_context="Food & Drink - Coffee Shops")])
        count = apply_quick_tap_classifications()
        assert count == 1
        t = get_transaction("tx_001")
        assert t["llm_category"] == "Food & Drink"
        assert t["llm_subcategory"] == "Coffee Shops"
        assert float(t["llm_confidence"]) == pytest.approx(1.0)
        assert t["llm_model"] == "quick-tap"

    def test_no_match_leaves_unclassified(self, db):
        pid = upsert_parent("Food & Drink")
        upsert_subcategory("Coffee Shops", pid)
        write_to_db([_txn(user_context="just a coffee, nothing formal")])
        count = apply_quick_tap_classifications()
        assert count == 0
        assert get_transaction("tx_001")["llm_category"] is None

    def test_skipped_transactions_ignored(self, db):
        pid = upsert_parent("Food & Drink")
        upsert_subcategory("Coffee Shops", pid)
        write_to_db([_txn(user_context="Food & Drink - Coffee Shops", skipped=True)])
        count = apply_quick_tap_classifications()
        assert count == 0

    def test_ambiguous_subcategory_name_resolved_by_parent(self, db):
        entertainment = upsert_parent("Entertainment")
        subscriptions = upsert_parent("Subscriptions & Software")
        upsert_subcategory("Streaming", entertainment)
        upsert_subcategory("Streaming", subscriptions)
        write_to_db([_txn(user_context="Subscriptions & Software - Streaming")])
        apply_quick_tap_classifications()
        t = get_transaction("tx_001")
        assert t["llm_category"] == "Subscriptions & Software"
        assert t["llm_subcategory"] == "Streaming"

    def test_does_not_reclassify_already_classified(self, db):
        pid = upsert_parent("Food & Drink")
        upsert_subcategory("Coffee Shops", pid)
        write_to_db([_txn(user_context="Food & Drink - Coffee Shops")])
        update_classification("tx_001", "Transport", "Fuel", 0.8, "existing-model")
        count = apply_quick_tap_classifications()
        assert count == 0
        t = get_transaction("tx_001")
        assert t["llm_category"] == "Transport"
        assert t["llm_model"] == "existing-model"

    def test_returns_count_of_classified(self, db):
        pid = upsert_parent("Food & Drink")
        upsert_subcategory("Coffee Shops", pid)
        write_to_db([
            _txn("tx_001", user_context="Food & Drink - Coffee Shops"),
            _txn("tx_002", user_context="Food & Drink - Coffee Shops"),
            _txn("tx_003", user_context="not a match"),
        ])
        assert apply_quick_tap_classifications() == 2


class TestHelpers:
    def test_ts_returns_value_when_truthy(self):
        assert _ts("2026-01-01") == "2026-01-01"
        assert _ts(42) == 42

    def test_ts_returns_none_for_falsy(self):
        assert _ts(None) is None
        assert _ts("") is None

    def test_rows_returns_list_of_dicts(self, db):
        write_to_db([_txn()])
        rows = _rows("SELECT id, amount FROM transactions")
        assert len(rows) == 1
        assert rows[0]["id"] == "tx_001"
        assert "amount" in rows[0]

    def test_rows_with_params(self, db):
        write_to_db([_txn("tx_001"), _txn("tx_002")])
        rows = _rows("SELECT id FROM transactions WHERE id = ?", ["tx_001"])
        assert len(rows) == 1
        assert rows[0]["id"] == "tx_001"


class TestBackups:
    @pytest.fixture(autouse=True)
    def _isolate_backup_dir(self, tmp_path, monkeypatch):
        """backup_dir() derives from DB_PATH, so pointing DB_PATH at tmp_path
        keeps every backup written by these tests out of the real data dir."""
        monkeypatch.setattr(database_functions, "DB_PATH", str(tmp_path / "test.db"))

    def test_creates_a_restorable_export(self, db, tmp_path):
        write_to_db([_txn(id="tx_backup", user_context="before the wipe")])
        target = database_functions.backup_db("test")

        assert (target / "schema.sql").is_file()
        assert (target / "load.sql").is_file()

        restored = duckdb.connect(":memory:")
        restored.execute(f"IMPORT DATABASE '{target}'")
        rows = restored.execute(
            "SELECT id, user_context FROM transactions"
        ).fetchall()
        restored.close()
        assert rows == [("tx_backup", "before the wipe")]

    def test_survives_wiping_the_live_data(self, db):
        """The whole point: the backup must still hold the rows after the
        destructive action it was taken to protect against."""
        write_to_db([_txn(id="tx_doomed")])
        target = database_functions.backup_db("wipe-taxonomy")
        get_con().execute("DELETE FROM transactions")

        restored = duckdb.connect(":memory:")
        restored.execute(f"IMPORT DATABASE '{target}'")
        count = restored.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        restored.close()
        assert count == 1

    def test_reason_appears_in_the_directory_name(self, db):
        target = database_functions.backup_db("delete-parent")
        assert target.name.endswith("_delete-parent")

    def test_unsafe_reason_characters_are_replaced(self, db):
        """The reason is built from category names, which are free text and can
        contain path separators (e.g. 'GP / Medical')."""
        target = database_functions.backup_db("GP / Medical")
        assert "/" not in target.name and "\\" not in target.name
        assert target.parent == database_functions.backup_dir()

    def test_two_backups_in_the_same_second_do_not_collide(self, db):
        write_to_db([_txn(id="tx_a")])
        first = database_functions.backup_db("same-second")
        second = database_functions.backup_db("same-second")
        assert first != second
        assert first.is_dir() and second.is_dir()

    def test_list_backups_is_newest_first(self, db):
        database_functions.backup_db("one")
        database_functions.backup_db("two")
        listed = database_functions.list_backups()
        assert len(listed) == 2
        assert listed == sorted(listed, reverse=True)

    def test_list_backups_is_empty_before_any_backup(self, db):
        assert database_functions.list_backups() == []

    def test_old_backups_are_pruned(self, db, monkeypatch):
        monkeypatch.setattr(database_functions, "BACKUPS_KEPT", 3)
        for i in range(5):
            database_functions.backup_db(f"run{i}")
        remaining = database_functions.list_backups()
        assert len(remaining) == 3
        # Pruning must drop the OLDEST, so the most recent run survives.
        assert any(p.name.endswith("_run4") for p in remaining)
        assert not any(p.name.endswith("_run0") for p in remaining)


class TestBackupManifest:
    @pytest.fixture(autouse=True)
    def _isolate_backup_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(database_functions, "DB_PATH", str(tmp_path / "test.db"))

    def test_records_what_the_backup_contains(self, db):
        write_to_db([_txn(id="tx_a", created="2025-03-01T10:00:00")])
        write_to_db([_txn(id="tx_b", created="2026-02-01T10:00:00")])
        update_classification("tx_a", "Food & Drink", "Coffee Shops", 1.0, "test")
        parent_id = upsert_parent("Food & Drink")
        upsert_subcategory("Coffee Shops", parent_id)
        upsert_subscription("Netflix", 9.99, "monthly", "Netflix")

        target = database_functions.backup_db("test")
        manifest = database_functions.read_backup_manifest(target)

        assert manifest["transactions"] == 2
        assert manifest["classified"] == 1
        assert manifest["reason"] == "test"
        assert manifest["parent_categories"] == 1
        assert manifest["subcategories"] == 1
        assert manifest["subscriptions"] == 1
        assert manifest["earliest"] == "2025-03-01"
        assert manifest["latest"] == "2026-02-01"

    def test_manifest_describes_the_state_before_the_destructive_action(self, db):
        """The counts have to reflect what was saved, not what is left behind."""
        write_to_db([_txn(id="tx_doomed")])
        target = database_functions.backup_db("wipe")
        get_con().execute("DELETE FROM transactions")
        assert database_functions.read_backup_manifest(target)["transactions"] == 1

    def test_empty_database_has_no_coverage_dates(self, db):
        target = database_functions.backup_db("empty")
        manifest = database_functions.read_backup_manifest(target)
        assert manifest["transactions"] == 0
        assert manifest["earliest"] is None and manifest["latest"] is None

    def test_missing_manifest_reads_as_empty(self, db, tmp_path):
        """Backups taken before manifests existed, and folders a human dropped
        into backups/ by hand, must list rather than raise."""
        stray = database_functions.backup_dir() / "hand-made-copy"
        stray.mkdir(parents=True)
        assert database_functions.read_backup_manifest(stray) == {}

    def test_corrupt_manifest_reads_as_empty(self, db):
        target = database_functions.backup_db("test")
        (target / database_functions.MANIFEST_NAME).write_text("{not json")
        assert database_functions.read_backup_manifest(target) == {}

    def test_size_counts_every_exported_file(self, db):
        write_to_db([_txn(id="tx_a")])
        target = database_functions.backup_db("test")
        size = database_functions.backup_size_bytes(target)
        assert size > 0
        assert size == sum(p.stat().st_size for p in target.rglob("*") if p.is_file())


class TestDeleteBackup:
    @pytest.fixture(autouse=True)
    def _isolate_backup_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(database_functions, "DB_PATH", str(tmp_path / "test.db"))

    def test_removes_only_the_chosen_backup(self, db):
        keep = database_functions.backup_db("keep")
        doomed = database_functions.backup_db("doomed")

        database_functions.delete_backup(doomed)

        assert not doomed.exists()
        assert keep.is_dir()
        assert database_functions.list_backups() == [keep]

    def test_accepts_a_string_path(self, db):
        """The dashboard round-trips the choice through session_state, which
        stores it as a string."""
        target = database_functions.backup_db("test")
        database_functions.delete_backup(str(target))
        assert not target.exists()

    def test_refuses_a_path_outside_the_backups_directory(self, db, tmp_path):
        """This ends in rmtree() on a value that arrives from a UI control --
        anything that isn't a backup folder must be refused, not deleted."""
        outsider = tmp_path / "important_data"
        outsider.mkdir()
        with pytest.raises(ValueError):
            database_functions.delete_backup(outsider)
        assert outsider.is_dir()

    def test_refuses_the_backups_directory_itself(self, db):
        database_functions.backup_db("test")
        root = database_functions.backup_dir()
        with pytest.raises(ValueError):
            database_functions.delete_backup(root)
        assert root.is_dir()

    def test_refuses_a_traversal_out_of_the_backups_directory(self, db, tmp_path):
        outsider = tmp_path / "important_data"
        outsider.mkdir()
        database_functions.backup_db("test")
        escape = database_functions.backup_dir() / ".." / "important_data"
        with pytest.raises(ValueError):
            database_functions.delete_backup(escape)
        assert outsider.is_dir()

    def test_refuses_a_nested_directory_rather_than_a_backup(self, db):
        """Only direct children of backups/ are backups; a subdirectory inside
        one is part of an export, not a deletable unit."""
        target = database_functions.backup_db("test")
        nested = target / "nested"
        nested.mkdir()
        with pytest.raises(ValueError):
            database_functions.delete_backup(nested)

    def test_refuses_a_backup_that_is_already_gone(self, db):
        target = database_functions.backup_db("test")
        database_functions.delete_backup(target)
        with pytest.raises(ValueError):
            database_functions.delete_backup(target)
