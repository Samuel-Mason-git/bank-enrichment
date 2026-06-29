import pytest

from database_functions import (
    get_con, write_to_db, get_transaction, get_all_transactions,
    get_unclassified, get_recent, get_stats, update_classification,
    upsert_parent, upsert_subcategory, get_parents, get_subcategories,
    get_subscriptions, upsert_subscription, toggle_subscription,
    delete_subscription, search, _ts, _rows,
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
