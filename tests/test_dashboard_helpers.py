import pandas as pd
import pytest

from dashboard_helpers import (
    FREQ_MONTHLY, pct_delta, detect_subscriptions, should_deactivate_subscription,
    sanitize_classification_edit,
)


def _txn_df(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal transactions-shaped DataFrame from partial row dicts."""
    defaults = {"merchant_name": None, "amount": 0.0, "skipped": False}
    df = pd.DataFrame([{**defaults, **r} for r in rows])
    df["created_at"] = pd.to_datetime(df["created_at"])
    return df


def _dates(start: str, count: int, step_days: int) -> list[str]:
    base = pd.Timestamp(start)
    return [(base + pd.Timedelta(days=step_days * i)).strftime("%Y-%m-%d") for i in range(count)]


class TestPctDelta:
    def test_zero_previous_returns_none(self):
        assert pct_delta(100, 0) is None

    def test_positive_delta(self):
        assert pct_delta(110, 100) == 10.0

    def test_negative_delta(self):
        assert pct_delta(90, 100) == -10.0

    def test_rounds_to_one_decimal(self):
        assert pct_delta(100, 97) == pytest.approx(3.1)


class TestDetectSubscriptions:
    def test_detects_monthly_cadence(self):
        df = _txn_df([
            {"created_at": d, "merchant_name": "Netflix", "amount": -9.99}
            for d in _dates("2026-01-01", 4, 30)
        ])
        candidates = detect_subscriptions(df, confirmed_names=set())
        assert len(candidates) == 1
        assert candidates[0]["name"] == "Netflix"
        assert candidates[0]["frequency"] == "monthly"
        assert candidates[0]["amount"] == 9.99

    def test_detects_weekly_cadence(self):
        df = _txn_df([
            {"created_at": d, "merchant_name": "Meal Kit Co", "amount": -25.00}
            for d in _dates("2026-01-01", 5, 7)
        ])
        candidates = detect_subscriptions(df, confirmed_names=set())
        assert len(candidates) == 1
        assert candidates[0]["frequency"] == "weekly"

    def test_irregular_spend_not_detected(self):
        # Highly variable gaps (2, 40, 5, 60 days) — not subscription-like
        df = _txn_df([
            {"created_at": d, "merchant_name": "Corner Shop", "amount": -4.50}
            for d in ["2026-01-01", "2026-01-03", "2026-02-12", "2026-02-17", "2026-04-18"]
        ])
        candidates = detect_subscriptions(df, confirmed_names=set())
        assert candidates == []

    def test_already_confirmed_merchant_excluded(self):
        df = _txn_df([
            {"created_at": d, "merchant_name": "Netflix", "amount": -9.99}
            for d in _dates("2026-01-01", 4, 30)
        ])
        candidates = detect_subscriptions(df, confirmed_names={"Netflix"})
        assert candidates == []

    def test_single_occurrence_excluded(self):
        df = _txn_df([{"created_at": "2026-01-01", "merchant_name": "Netflix", "amount": -9.99}])
        candidates = detect_subscriptions(df, confirmed_names=set())
        assert candidates == []

    def test_skipped_transactions_excluded(self):
        df = _txn_df([
            {"created_at": d, "merchant_name": "Netflix", "amount": -9.99, "skipped": True}
            for d in _dates("2026-01-01", 4, 30)
        ])
        candidates = detect_subscriptions(df, confirmed_names=set())
        assert candidates == []

    def test_income_not_treated_as_subscription(self):
        df = _txn_df([
            {"created_at": d, "merchant_name": "Employer", "amount": 2000.0}
            for d in _dates("2026-01-01", 4, 30)
        ])
        candidates = detect_subscriptions(df, confirmed_names=set())
        assert candidates == []

    def test_monthly_cost_uses_freq_monthly_table(self):
        df = _txn_df([
            {"created_at": d, "merchant_name": "Annual Mag", "amount": -60.0}
            for d in _dates("2024-01-01", 3, 365)
        ])
        candidates = detect_subscriptions(df, confirmed_names=set())
        assert len(candidates) == 1
        assert candidates[0]["monthly_cost"] == pytest.approx(60.0 * FREQ_MONTHLY["annual"])


class TestShouldDeactivateSubscription:
    def test_recent_transaction_keeps_active(self):
        df = _txn_df([{"created_at": "2026-06-20", "merchant_name": "Netflix", "amount": -9.99}])
        cutoff = pd.Timestamp("2026-06-01")
        sub = {"merchant_name": "Netflix", "name": "Netflix"}
        assert should_deactivate_subscription(sub, df, cutoff) is False

    def test_only_old_transactions_deactivates(self):
        df = _txn_df([{"created_at": "2026-01-01", "merchant_name": "Netflix", "amount": -9.99}])
        cutoff = pd.Timestamp("2026-06-01")
        sub = {"merchant_name": "Netflix", "name": "Netflix"}
        assert should_deactivate_subscription(sub, df, cutoff) is True

    def test_no_matching_transactions_deactivates(self):
        df = _txn_df([{"created_at": "2026-06-20", "merchant_name": "Spotify", "amount": -9.99}])
        cutoff = pd.Timestamp("2026-06-01")
        sub = {"merchant_name": "Netflix", "name": "Netflix"}
        assert should_deactivate_subscription(sub, df, cutoff) is True

    def test_falls_back_to_name_when_no_merchant_name(self):
        df = _txn_df([{"created_at": "2026-06-20", "merchant_name": "Netflix Inc", "amount": -9.99}])
        cutoff = pd.Timestamp("2026-06-01")
        sub = {"merchant_name": None, "name": "Netflix"}
        assert should_deactivate_subscription(sub, df, cutoff) is False

    def test_regex_special_characters_do_not_crash(self):
        """Regression test: merchant names with regex-special characters (e.g. an
        unbalanced parenthesis) used to crash with ArrowInvalid before this was
        fixed to use a literal substring match instead of a regex."""
        df = _txn_df([{"created_at": "2026-06-20", "merchant_name": "EE (UK) Mobile", "amount": -20.0}])
        cutoff = pd.Timestamp("2026-06-01")
        sub = {"merchant_name": "EE (UK", "name": "EE (UK"}
        assert should_deactivate_subscription(sub, df, cutoff) is False

    def test_regex_special_characters_do_not_false_match(self):
        """'3.99' must not match '3x99' — '.' is not a wildcard here."""
        df = _txn_df([{"created_at": "2026-06-20", "merchant_name": "3x99 Deals", "amount": -5.0}])
        cutoff = pd.Timestamp("2026-06-01")
        sub = {"merchant_name": "3.99", "name": "3.99"}
        assert should_deactivate_subscription(sub, df, cutoff) is True


class TestSanitizeClassificationEdit:
    def test_passes_through_valid_category_and_subcategory(self):
        assert sanitize_classification_edit("Food & Drink", "Groceries") == ("Food & Drink", "Groceries")

    def test_empty_subcategory_becomes_none(self):
        assert sanitize_classification_edit("Food & Drink", "") == ("Food & Drink", None)

    def test_clearing_category_also_clears_subcategory(self):
        """Regression test: a subcategory can't exist without a parent — saving
        one without the other used to create an orphaned label invisible to
        every category-based view."""
        assert sanitize_classification_edit("", "Groceries") == (None, None)

    def test_clearing_category_with_none_subcategory(self):
        assert sanitize_classification_edit(None, None) == (None, None)

    def test_both_empty_stays_empty(self):
        assert sanitize_classification_edit("", "") == (None, None)
