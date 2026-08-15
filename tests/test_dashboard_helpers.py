from pathlib import Path

import pandas as pd
import pytest

from dashboard_helpers import (
    FREQ_MONTHLY, pct_delta, detect_subscriptions, should_deactivate_subscription,
    sanitize_classification_edit, role_breakdown, merchant_display_name,
    backup_label, backup_reason, backup_coverage, format_bytes,
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


class TestMerchantDisplayName:
    def test_prefers_merchant_name(self):
        df = pd.DataFrame({"merchant_name": ["Tesco"], "counterparty_name": ["TESCO PLC"]})
        assert merchant_display_name(df).tolist() == ["Tesco"]

    def test_falls_back_to_counterparty_when_merchant_null(self):
        df = pd.DataFrame({"merchant_name": [None], "counterparty_name": ["Landlord"]})
        assert merchant_display_name(df).tolist() == ["Landlord"]

    def test_falls_back_to_counterparty_when_merchant_empty_string(self):
        """Monzo sends merchant_name='' (not NULL) for many direct debits and
        bank transfers -- fillna() alone leaves the empty string in place, which
        silently drops those rows out of every merchant-based grouping."""
        df = pd.DataFrame({"merchant_name": [""], "counterparty_name": ["Landlord"]})
        assert merchant_display_name(df).tolist() == ["Landlord"]

    def test_null_when_neither_is_present(self):
        df = pd.DataFrame({"merchant_name": [""], "counterparty_name": [None]})
        assert merchant_display_name(df).isna().tolist() == [True]

    def test_works_without_counterparty_column(self):
        """Some callers pass a slimmed-down frame with no counterparty_name."""
        df = pd.DataFrame({"merchant_name": ["Tesco", ""]})
        result = merchant_display_name(df)
        assert result.tolist()[0] == "Tesco"
        assert pd.isna(result.tolist()[1])

    def test_empty_frame_does_not_raise(self):
        df = pd.DataFrame({"merchant_name": [], "counterparty_name": []})
        assert merchant_display_name(df).tolist() == []


class TestDetectSubscriptions:
    def test_detects_monthly_cadence(self):
        df = _txn_df([
            {"created_at": d, "merchant_name": "Netflix", "amount": -9.99}
            for d in _dates("2026-01-01", 4, 30)
        ])
        candidates = detect_subscriptions(df, confirmed_names=set(), reference_date=pd.Timestamp("2026-04-05"))
        assert len(candidates) == 1
        assert candidates[0]["name"] == "Netflix"
        assert candidates[0]["frequency"] == "monthly"
        assert candidates[0]["amount"] == 9.99

    def test_detects_weekly_cadence(self):
        df = _txn_df([
            {"created_at": d, "merchant_name": "Meal Kit Co", "amount": -25.00}
            for d in _dates("2026-01-01", 5, 7)
        ])
        candidates = detect_subscriptions(df, confirmed_names=set(), reference_date=pd.Timestamp("2026-02-01"))
        assert len(candidates) == 1
        assert candidates[0]["frequency"] == "weekly"

    def test_few_occurrences_not_enough_for_fast_cadence(self):
        """Regression test: two occurrences spaced ~7 days apart (one gap) is
        weak evidence of a real weekly habit -- it's just as consistent with
        two coincidental one-off visits to a shop. Faster cadences (weekly/
        fortnightly) now require more supporting occurrences before being
        suggested (the exact real-world case that triggered this: a
        physical shop visited 3 times in quick succession, never again, got
        suggested as a "weekly subscription")."""
        df = _txn_df([
            {"created_at": d, "merchant_name": "B&M", "amount": -5.00}
            for d in _dates("2026-01-01", 3, 7)
        ])
        candidates = detect_subscriptions(df, confirmed_names=set(), reference_date=pd.Timestamp("2026-01-16"))
        assert candidates == []

    def test_stale_pattern_not_suggested(self):
        """Regression test: a cadence-matching pattern whose last occurrence
        is long past its expected interval is a lapsed one-off, not a
        currently-active subscription worth suggesting -- e.g. weekly-spaced
        visits to a shop the user "hasn't been to in ages" shouldn't still
        show up as a suggested subscription months later."""
        df = _txn_df([
            {"created_at": d, "merchant_name": "B&M", "amount": -5.00}
            for d in _dates("2026-01-01", 4, 7)
        ])
        # Enough occurrences to pass the count bar, but reference_date is
        # months after the last transaction -- well outside weekly's window.
        candidates = detect_subscriptions(df, confirmed_names=set(), reference_date=pd.Timestamp("2026-06-01"))
        assert candidates == []

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

    def test_detects_via_counterparty_name_when_merchant_name_missing(self):
        """Regression test: many direct debits/bank transfers never populate
        merchant_name at all, reporting counterparty_name instead -- a
        subscription paid that way used to be invisible to detection
        entirely, since the old filter required merchant_name.notna()."""
        df = _txn_df([
            {"created_at": d, "merchant_name": None, "counterparty_name": "NETFLIX.COM", "amount": -9.99}
            for d in _dates("2026-01-01", 4, 30)
        ])
        candidates = detect_subscriptions(df, confirmed_names=set(), reference_date=pd.Timestamp("2026-04-05"))
        assert len(candidates) == 1
        assert candidates[0]["name"] == "NETFLIX.COM"
        assert candidates[0]["frequency"] == "monthly"

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
        candidates = detect_subscriptions(df, confirmed_names=set(), reference_date=pd.Timestamp("2026-01-10"))
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

    def test_recent_transaction_via_counterparty_name_keeps_active(self):
        """Regression test: a subscription paid by direct debit may only ever
        show up via counterparty_name (merchant_name null) -- matching
        against merchant_name alone used to make this look like it had no
        recent transaction at all, silently auto-deactivating a subscription
        that's actually still active."""
        df = _txn_df([{
            "created_at": "2026-06-20", "merchant_name": None,
            "counterparty_name": "NETFLIX.COM", "amount": -9.99,
        }])
        cutoff = pd.Timestamp("2026-06-01")
        sub = {"merchant_name": "NETFLIX.COM", "name": "Netflix"}
        assert should_deactivate_subscription(sub, df, cutoff) is False


def _role_df(rows: list[dict]) -> pd.DataFrame:
    defaults = {"llm_category": None, "llm_subcategory": None, "month": None}
    return pd.DataFrame([{**defaults, **r} for r in rows])


class TestRoleBreakdown:
    def test_filters_by_role(self):
        df = _role_df([
            {"role": "spend", "amount": -10.0, "llm_category": "Food & Drink"},
            {"role": "income", "amount": 100.0, "llm_category": "Income"},
        ])
        result = role_breakdown(df, ("spend",), "llm_category")
        assert list(result["Category"]) == ["Food & Drink"]
        assert result["Amount"].iloc[0] == 10.0

    def test_multiple_roles(self):
        df = _role_df([
            {"role": "transfer", "amount": 20.0, "llm_category": "Transfers"},
            {"role": "excluded", "amount": 5.0, "llm_category": "Income"},
            {"role": "spend", "amount": -10.0, "llm_category": "Food & Drink"},
        ])
        result = role_breakdown(df, ("transfer", "excluded"), "llm_category")
        assert set(result["Category"]) == {"Transfers", "Income"}

    def test_sign_negative_excludes_positive_rows(self):
        """Regression test: a bidirectional role like "transfer" can have both
        an Inbound (positive) and Outbound (negative) subcategory. Without
        restricting to one sign, an "Outgoings" chart would show an Inbound
        Transfer bar alongside Outbound -- money coming in, mislabeled as
        something going out."""
        df = _role_df([
            {"role": "transfer", "amount": -674.0, "llm_category": "Transfers", "llm_subcategory": "Outbound Transfer"},
            {"role": "transfer", "amount": 540.0, "llm_category": "Transfers", "llm_subcategory": "Inbound Transfer"},
        ])
        result = role_breakdown(df, ("transfer",), "llm_subcategory", sign="negative")
        assert list(result["Subcategory"]) == ["Outbound Transfer"]
        assert result["Amount"].iloc[0] == 674.0

    def test_sign_positive_excludes_negative_rows(self):
        df = _role_df([
            {"role": "transfer", "amount": -674.0, "llm_category": "Transfers", "llm_subcategory": "Outbound Transfer"},
            {"role": "transfer", "amount": 540.0, "llm_category": "Transfers", "llm_subcategory": "Inbound Transfer"},
        ])
        result = role_breakdown(df, ("transfer",), "llm_subcategory", sign="positive")
        assert list(result["Subcategory"]) == ["Inbound Transfer"]
        assert result["Amount"].iloc[0] == 540.0

    def test_sums_and_sorts_descending_by_amount(self):
        df = _role_df([
            {"role": "spend", "amount": -5.0, "llm_category": "Small"},
            {"role": "spend", "amount": -50.0, "llm_category": "Big"},
            {"role": "spend", "amount": -3.0, "llm_category": "Small"},
        ])
        result = role_breakdown(df, ("spend",), "llm_category")
        assert list(result["Category"]) == ["Big", "Small"]
        assert result.loc[result["Category"] == "Small", "Amount"].iloc[0] == 8.0

    def test_missing_category_becomes_unclassified(self):
        df = _role_df([{"role": "spend", "amount": -10.0, "llm_category": None}])
        result = role_breakdown(df, ("spend",), "llm_category")
        assert list(result["Category"]) == ["Unclassified"]

    def test_empty_input_returns_empty_frame_with_expected_columns(self):
        df = _role_df([{"role": "income", "amount": 100.0, "llm_category": "Income"}])
        result = role_breakdown(df, ("spend",), "llm_category")
        assert result.empty
        assert list(result.columns) == ["Category", "Amount"]

    def test_subcategory_grouping_uses_subcategory_label(self):
        df = _role_df([
            {"role": "spend", "amount": -10.0, "llm_category": "Food & Drink", "llm_subcategory": "Groceries"},
        ])
        result = role_breakdown(df, ("spend",), "llm_subcategory")
        assert list(result.columns) == ["Subcategory", "Amount"]
        assert list(result["Subcategory"]) == ["Groceries"]

    def test_by_month_groups_by_month_and_keeps_month_order(self):
        df = _role_df([
            {"role": "spend", "amount": -10.0, "llm_category": "Food & Drink", "month": "2026-02"},
            {"role": "spend", "amount": -20.0, "llm_category": "Food & Drink", "month": "2026-01"},
        ])
        result = role_breakdown(df, ("spend",), "llm_category", by_month=True)
        assert list(result.columns) == ["Month", "Category", "Amount"]
        assert list(result["Month"]) == ["2026-01", "2026-02"], "month grouping should not be amount-sorted"


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


class TestBackupNameFormatting:
    def test_label_is_a_readable_timestamp(self):
        assert backup_label(Path("/b/20260813-214233_manual")) == "2026-08-13 21:42"

    def test_label_falls_back_to_the_folder_name_when_unparseable(self):
        """A hand-made or renamed folder in backups/ should still be listed
        rather than taking down the Settings tab."""
        assert backup_label(Path("/b/my-own-copy")) == "my-own-copy"

    def test_reason_is_extracted(self):
        assert backup_reason(Path("/b/20260813-214233_wipe-taxonomy")) == "wipe-taxonomy"

    def test_reason_keeps_underscores_from_the_category_name(self):
        """Only the first underscore separates stamp from reason -- a category
        name may contain its own."""
        assert backup_reason(Path("/b/20260813-214233_delete-parent-Bills_Utils")) == \
            "delete-parent-Bills_Utils"

    def test_same_second_collision_suffix_is_hidden(self):
        assert backup_reason(Path("/b/20260813-214233_wipe-labels-2")) == "wipe-labels"

    def test_trailing_digits_that_are_part_of_the_name_are_kept(self):
        assert backup_reason(Path("/b/20260813-214233_manual")) == "manual"

    def test_missing_reason_shows_a_dash(self):
        assert backup_reason(Path("/b/20260813-214233")) == "—"


class TestFormatBytes:
    def test_bytes_have_no_decimal(self):
        assert format_bytes(512) == "512 B"

    def test_scales_to_kilobytes(self):
        assert format_bytes(2048) == "2.0 KB"

    def test_scales_to_megabytes(self):
        assert format_bytes(5 * 1024 * 1024) == "5.0 MB"

    def test_scales_to_gigabytes(self):
        assert format_bytes(3 * 1024 ** 3) == "3.0 GB"

    def test_zero_and_none_show_a_dash(self):
        assert format_bytes(0) == "—"
        assert format_bytes(None) == "—"


class TestBackupCoverage:
    def test_renders_a_range(self):
        assert backup_coverage({"earliest": "2025-01-01", "latest": "2026-08-13"}) == \
            "2025-01-01 → 2026-08-13"

    def test_single_day_is_not_shown_as_a_range(self):
        assert backup_coverage({"earliest": "2026-08-13", "latest": "2026-08-13"}) == "2026-08-13"

    def test_empty_manifest_shows_a_dash(self):
        """A backup taken before manifests existed still has to render."""
        assert backup_coverage({}) == "—"

    def test_backup_of_an_empty_database_shows_a_dash(self):
        assert backup_coverage({"earliest": None, "latest": None}) == "—"
