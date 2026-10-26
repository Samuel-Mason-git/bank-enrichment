import pytest

import server_db
from server_db import get_con, get_quick_categories, record_context, record_label, record_request
from tests.server_helpers import (
    add_label, add_transaction, label_sources, requests_sent, seed_taxonomy, transaction,
)


class TestGetCon:
    def test_raises_before_init(self):
        with pytest.raises(RuntimeError, match="not initialised"):
            get_con()

    def test_returns_connection_after_fixture(self, server_con):
        assert get_con() is server_con

    def test_reconnects_when_the_connection_has_dropped(self, server_con, monkeypatch):
        server_db._con = type("Dropped", (), {"closed": True, "broken": False})()
        monkeypatch.setattr(server_db, "_connect", lambda: server_con)
        assert get_con() is server_con


def _labelled(con, merchant, sub_id, times, start=[0]):
    for _ in range(times):
        start[0] += 1
        add_transaction(con, f"t{start[0]}", merchant=merchant)
        add_label(con, f"t{start[0]}", sub_id)


class TestGetQuickCategories:
    def test_the_merchants_own_most_used_categories(self, server_con):
        seed_taxonomy(server_con)
        _labelled(server_con, "Tesco", 1, 3)
        _labelled(server_con, "Tesco", 2, 1)
        _labelled(server_con, "Greggs", 2, 5)
        result = get_quick_categories("Tesco")
        assert [r["subcategory"] for r in result] == ["Groceries", "Takeaway"]

    def test_the_id_is_the_subcategorys(self, server_con):
        seed_taxonomy(server_con)
        _labelled(server_con, "Tesco", 2, 1)
        assert get_quick_categories("Tesco")[0]["id"] == 2

    def test_falls_back_to_the_top_overall_for_an_unknown_merchant(self, server_con):
        seed_taxonomy(server_con)
        _labelled(server_con, "Greggs", 2, 2)
        result = get_quick_categories("Unknown Merchant")
        assert [r["subcategory"] for r in result] == ["Takeaway"]

    def test_empty_when_nothing_is_labelled(self, server_con):
        assert get_quick_categories(None) == []
        assert get_quick_categories("Tesco") == []

    def test_merchant_results_capped_at_three_and_overall_at_five(self, server_con):
        seed_taxonomy(server_con)
        for sub_id in (1, 2, 3, 4):
            _labelled(server_con, "Tesco", sub_id, 1)
        assert len(get_quick_categories("Tesco")) == 3
        assert len(get_quick_categories(None)) == 4  # only four exist


class TestRecordingHistory:
    def test_context_is_the_latest(self, server_con):
        add_transaction(server_con)
        record_context(server_con, "tx_1", "first", source="telegram")
        record_context(server_con, "tx_1", "second", source="dashboard")
        assert transaction(server_con)["user_context"] == "second"

    def test_a_corrected_date_is_carried_forward(self, server_con):
        add_transaction(server_con)
        server_con.execute("INSERT INTO context_events (txn_id, user_context, created_at_override, source) "
                           "VALUES ('tx_1', 'x', '2026-09-30 09:00', 'dashboard')")
        record_context(server_con, "tx_1", "y", source="telegram")
        assert str(transaction(server_con)["created_at"]) == "2026-09-30 10:00:00"  # 09:00 UTC, in London (BST)

    def test_your_context_clears_a_rules_label(self, server_con):
        seed_taxonomy(server_con)
        add_transaction(server_con)
        add_label(server_con, "tx_1", 1, source="rule")
        record_context(server_con, "tx_1", "actually a gift", source="telegram")
        assert transaction(server_con)["llm_category"] is None
        assert label_sources(server_con) == ["rule", "manual"]

    def test_but_not_anyone_elses(self, server_con):
        seed_taxonomy(server_con)
        add_transaction(server_con)
        add_label(server_con, "tx_1", 1, source="llm")
        record_context(server_con, "tx_1", "note", source="telegram")
        assert transaction(server_con)["llm_subcategory"] == "Groceries"

    def test_a_request_isnt_context(self, server_con):
        add_transaction(server_con)
        record_request(server_con, "tx_1")
        assert transaction(server_con)["enriched_at"] is None
        assert requests_sent(server_con) == 1

    def test_record_label(self, server_con):
        seed_taxonomy(server_con)
        add_transaction(server_con)
        record_label(server_con, "tx_1", 1, 2, source="rule", model="rule")
        assert transaction(server_con)["llm_subcategory"] == "Takeaway"
