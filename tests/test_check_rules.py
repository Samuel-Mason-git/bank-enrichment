"""Tests for the pure matching logic in server_scripts/check_rules.py."""
import pytest
from unittest.mock import MagicMock

from check_rules import _matches, _extract_field


# ── _matches ──────────────────────────────────────────────────────────────────

class TestMatchesExact:
    def test_equal_strings(self):
        assert _matches("Tesco", "exact", "Tesco") is True

    def test_case_insensitive(self):
        assert _matches("TESCO", "exact", "tesco") is True

    def test_not_equal(self):
        assert _matches("Tesco", "exact", "Asda") is False

    def test_partial_does_not_match(self):
        assert _matches("Tesco Superstore", "exact", "Tesco") is False


class TestMatchesContains:
    def test_substring_found(self):
        assert _matches("Tesco Superstore", "contains", "Tesco") is True

    def test_case_insensitive(self):
        assert _matches("TESCO SUPERSTORE", "contains", "tesco") is True

    def test_not_found(self):
        assert _matches("Sainsburys", "contains", "Tesco") is False

    def test_full_string_matches(self):
        assert _matches("Tesco", "contains", "Tesco") is True


class TestMatchesRegex:
    def test_simple_pattern(self):
        assert _matches("Netflix Monthly", "regex", r"Netflix") is True

    def test_case_insensitive(self):
        assert _matches("NETFLIX", "regex", r"netflix") is True

    def test_pattern_not_found(self):
        assert _matches("Spotify", "regex", r"Netflix") is False

    def test_anchored_pattern(self):
        assert _matches("Tesco Extra", "regex", r"^Tesco") is True
        assert _matches("Big Tesco", "regex", r"^Tesco") is False

    def test_digit_pattern(self):
        assert _matches("Order #12345", "regex", r"\d+") is True


class TestMatchesAmountRange:
    def test_within_range(self):
        # amount field is stored in pence as int; match_value is "low-high" in pounds
        assert _matches(1000, "amount_range", "5.00-20.00") is True  # £10.00

    def test_at_lower_bound(self):
        assert _matches(500, "amount_range", "5.00-20.00") is True  # £5.00 exactly

    def test_at_upper_bound(self):
        assert _matches(2000, "amount_range", "5.00-20.00") is True  # £20.00 exactly

    def test_below_range(self):
        assert _matches(499, "amount_range", "5.00-20.00") is False  # £4.99

    def test_above_range(self):
        assert _matches(2001, "amount_range", "5.00-20.00") is False  # £20.01

    def test_negative_amount_uses_abs(self):
        # Negative amounts (spend) should also match when abs value is in range
        assert _matches(-1000, "amount_range", "5.00-20.00") is True


class TestMatchesAmountExact:
    def test_exact_match(self):
        assert _matches(999, "amount_exact", "9.99") is True

    def test_no_match(self):
        assert _matches(1000, "amount_exact", "9.99") is False

    def test_negative_amount_uses_abs(self):
        assert _matches(-999, "amount_exact", "9.99") is True


class TestMatchesUnknownType:
    def test_unknown_type_returns_false(self):
        assert _matches("anything", "unknown_type", "value") is False


# ── _extract_field ─────────────────────────────────────────────────────────────

def _data(**kwargs):
    """Build a mock data object with the given attributes."""
    obj = MagicMock()
    obj.description = kwargs.get("description", "Test purchase")
    obj.category = kwargs.get("category", "shopping")
    obj.amount = kwargs.get("amount", 1000)
    merchant = kwargs.get("merchant")
    if merchant is None:
        obj.merchant = None
    else:
        obj.merchant = {"name": merchant}
    counterparty = kwargs.get("counterparty")
    if counterparty is None:
        obj.counterparty = None
    else:
        obj.counterparty = {"name": counterparty}
    return obj


class TestExtractField:
    def test_merchant_name(self):
        data = _data(merchant="Tesco")
        assert _extract_field(data, "merchant_name") == "Tesco"

    def test_merchant_name_when_no_merchant(self):
        data = _data(merchant=None)
        assert _extract_field(data, "merchant_name") is None

    def test_description(self):
        data = _data(description="Coffee at Costa")
        assert _extract_field(data, "description") == "Coffee at Costa"

    def test_category(self):
        data = _data(category="eating_out")
        assert _extract_field(data, "category") == "eating_out"

    def test_counterparty_name(self):
        data = _data(counterparty="John Smith")
        assert _extract_field(data, "counterparty_name") == "John Smith"

    def test_counterparty_name_when_no_counterparty(self):
        data = _data(counterparty=None)
        assert _extract_field(data, "counterparty_name") is None

    def test_amount(self):
        data = _data(amount=999)
        assert _extract_field(data, "amount") == 999

    def test_unknown_field_returns_none(self):
        data = _data()
        assert _extract_field(data, "nonexistent_field") is None
