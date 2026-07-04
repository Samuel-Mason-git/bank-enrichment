import pytest

from server_db import get_con, get_quick_categories


def _insert(con, id, category, subcategory, merchant_name, rank):
    con.execute(
        "INSERT INTO quick_categories (id, category, subcategory, merchant_name, rank) VALUES (?, ?, ?, ?, ?)",
        [id, category, subcategory, merchant_name, rank]
    )


class TestGetCon:
    def test_raises_before_init(self):
        with pytest.raises(RuntimeError, match="not initialised"):
            get_con()

    def test_returns_connection_after_fixture(self, server_con):
        assert get_con() is server_con


class TestGetQuickCategories:
    def test_returns_global_top_entries_when_no_merchant(self, server_con):
        for i in range(5):
            _insert(server_con, i, "Food & Drink", f"Sub{i}", None, i)
        result = get_quick_categories(None)
        assert len(result) == 5
        assert result[0]["subcategory"] == "Sub0"

    def test_returns_merchant_specific_when_matched(self, server_con):
        _insert(server_con, 1, "Food & Drink", "Groceries", "Tesco", 0)
        _insert(server_con, 2, "Food & Drink", "Snacks", "Tesco", 1)
        _insert(server_con, 3, "Food & Drink", "Coffee Shops", None, 0)
        result = get_quick_categories("Tesco")
        assert [r["subcategory"] for r in result] == ["Groceries", "Snacks"]

    def test_falls_back_to_global_when_merchant_not_found(self, server_con):
        _insert(server_con, 1, "Food & Drink", "Coffee Shops", None, 0)
        result = get_quick_categories("Unknown Merchant")
        assert len(result) == 1
        assert result[0]["subcategory"] == "Coffee Shops"

    def test_empty_when_no_data(self, server_con):
        assert get_quick_categories(None) == []
        assert get_quick_categories("Tesco") == []

    def test_merchant_results_capped_at_three(self, server_con):
        for i in range(5):
            _insert(server_con, i, "Food & Drink", f"Sub{i}", "Tesco", i)
        result = get_quick_categories("Tesco")
        assert len(result) == 3

    def test_global_results_capped_at_five(self, server_con):
        for i in range(10):
            _insert(server_con, i, "Food & Drink", f"Sub{i}", None, i)
        result = get_quick_categories(None)
        assert len(result) == 5

    def test_orders_by_rank(self, server_con):
        _insert(server_con, 1, "Food & Drink", "Second", "Tesco", 1)
        _insert(server_con, 2, "Food & Drink", "First", "Tesco", 0)
        result = get_quick_categories("Tesco")
        assert [r["subcategory"] for r in result] == ["First", "Second"]
