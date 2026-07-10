"""Tests for weekly_summary and monthly_summary pure functions and DB queries."""
import re
import pytest
from datetime import date, timedelta

from weekly_summary import (
    _week_key, _week_bounds, _prev_week,
    format_weekly_message, get_missing_weeks, get_week_stats,
)
from monthly_summary import format_monthly_message, get_missing_months


# ── Week key helpers ───────────────────────────────────────────────────────────

class TestWeekKey:
    def test_format_is_yyyy_wnn(self):
        result = _week_key(date(2026, 6, 29))
        assert re.match(r"^\d{4}-W\d{2}$", result)

    def test_known_monday(self):
        # 2026-06-29 is a Monday, ISO week 27
        assert _week_key(date(2026, 6, 29)) == "2026-W27"

    def test_sunday_same_week_as_monday(self):
        # 2026-07-05 is Sunday, still week 27
        assert _week_key(date(2026, 7, 5)) == "2026-W27"

    def test_consecutive_mondays_give_consecutive_weeks(self):
        d1 = date(2026, 6, 22)  # Monday
        d2 = date(2026, 6, 29)  # Monday
        y1, w1 = _week_key(d1).split("-W")
        y2, w2 = _week_key(d2).split("-W")
        assert (int(y2), int(w2)) > (int(y1), int(w1))


class TestWeekBounds:
    def test_monday_is_weekday_0(self):
        monday, _ = _week_bounds("2026-W27")
        assert monday.weekday() == 0

    def test_sunday_is_weekday_6(self):
        _, sunday = _week_bounds("2026-W27")
        assert sunday.weekday() == 6

    def test_span_is_6_days(self):
        monday, sunday = _week_bounds("2026-W27")
        assert (sunday - monday).days == 6

    def test_round_trip_with_week_key(self):
        key = "2026-W27"
        monday, _ = _week_bounds(key)
        assert _week_key(monday) == key

    def test_known_bounds(self):
        monday, sunday = _week_bounds("2026-W27")
        assert monday == date(2026, 6, 29)
        assert sunday == date(2026, 7, 5)


class TestPrevWeek:
    def test_decrements_within_year(self):
        assert _prev_week("2026-W27") == "2026-W26"
        assert _prev_week("2026-W10") == "2026-W09"

    def test_crosses_year_boundary(self):
        # Monday of 2026-W01 is 2025-12-29; prev is 2025-W52
        prev = _prev_week("2026-W01")
        monday_1, _ = _week_bounds("2026-W01")
        monday_prev, _ = _week_bounds(prev)
        assert (monday_1 - monday_prev).days == 7

    def test_prev_of_prev_is_two_weeks_back(self):
        key = "2026-W27"
        two_back = _prev_week(_prev_week(key))
        m_key, _ = _week_bounds(key)
        m_two, _ = _week_bounds(two_back)
        assert (m_key - m_two).days == 14


# ── Format weekly message ──────────────────────────────────────────────────────

def _week_stats(**overrides):
    base = {
        "week": "2026-W27",
        "monday": date(2026, 6, 29),
        "sunday": date(2026, 7, 5),
        "total_spend": 50.00,
        "total_income": 100.00,
        "total_invested": 0.0,
        "total_transferred": 0.0,
        "net": 50.00,
        "unclassified_spend": 0.0,
        "unclassified_income": 0.0,
        "spend_by_category": [],
        "income_by_category": [],
    }
    return {**base, **overrides}


class TestFormatWeeklyMessage:
    def test_contains_week_number(self):
        assert "27" in format_weekly_message(_week_stats())

    def test_contains_year(self):
        assert "2026" in format_weekly_message(_week_stats())

    def test_shows_spend_and_income(self):
        msg = format_weekly_message(_week_stats(total_spend=42.50, total_income=200.0))
        assert "42.50" in msg
        assert "200.00" in msg

    def test_positive_net_uses_up_emoji(self):
        assert "📈" in format_weekly_message(_week_stats(net=10.0))

    def test_negative_net_uses_down_emoji(self):
        assert "📉" in format_weekly_message(_week_stats(net=-5.0))

    def test_zero_net_uses_up_emoji(self):
        assert "📈" in format_weekly_message(_week_stats(net=0.0))

    def test_spend_categories_listed(self):
        stats = _week_stats(spend_by_category=[{"category": "Food & Drink", "amount": 30.0}])
        msg = format_weekly_message(stats)
        assert "Food & Drink" in msg
        assert "30.00" in msg

    def test_income_categories_listed(self):
        stats = _week_stats(income_by_category=[{"category": "Salary", "amount": 1000.0}])
        msg = format_weekly_message(stats)
        assert "Salary" in msg

    def test_unclassified_spend_shown_when_above_threshold(self):
        stats = _week_stats(unclassified_spend=5.50)
        msg = format_weekly_message(stats)
        assert "Unclassified" in msg
        assert "5.50" in msg

    def test_unclassified_spend_hidden_when_zero(self):
        msg = format_weekly_message(_week_stats(unclassified_spend=0.0))
        assert "Unclassified" not in msg

    def test_date_range_in_message(self):
        msg = format_weekly_message(_week_stats())
        # Should mention June and Jul (the Monday and Sunday of W27)
        assert "Jun" in msg or "Jul" in msg


# ── Format monthly message ─────────────────────────────────────────────────────

def _month_stats(**overrides):
    base = {
        "month": "2026-05",
        "total_spend": 500.00,
        "total_income": 2000.00,
        "total_invested": 0.0,
        "total_transferred": 0.0,
        "net": 1500.00,
        "spend_by_category": [],
        "income_by_category": [],
    }
    return {**base, **overrides}


class TestFormatMonthlyMessage:
    def test_contains_month_name(self):
        assert "May 2026" in format_monthly_message(_month_stats())

    def test_shows_spend_and_income(self):
        msg = format_monthly_message(_month_stats())
        assert "500.00" in msg
        assert "2000.00" in msg

    def test_positive_net_uses_up_emoji(self):
        assert "📈" in format_monthly_message(_month_stats(net=1500.0))

    def test_negative_net_uses_down_emoji(self):
        assert "📉" in format_monthly_message(_month_stats(net=-100.0))

    def test_spend_categories_listed(self):
        stats = _month_stats(spend_by_category=[{"category": "Shopping", "amount": 200.0}])
        msg = format_monthly_message(stats)
        assert "Shopping" in msg
        assert "200.00" in msg

    def test_income_categories_listed(self):
        stats = _month_stats(income_by_category=[{"category": "Salary", "amount": 2000.0}])
        assert "Salary" in format_monthly_message(stats)

    def test_no_categories_section_when_empty(self):
        msg = format_monthly_message(_month_stats())
        assert "Spend by category" not in msg

    def test_different_months_format_correctly(self):
        for month_str, expected in [("2026-01", "January"), ("2026-12", "December")]:
            assert expected in format_monthly_message(_month_stats(month="2026-01" if month_str == "2026-01" else "2026-12"))


# ── DB-dependent: get_missing_weeks / get_missing_months ──────────────────────

class TestGetMissingWeeks:
    def test_empty_when_no_transactions(self, db):
        assert get_missing_weeks() == []

    def test_returns_missing_completed_weeks(self, db):
        # Insert a transaction far in the past so at least one week is "completed"
        db.execute(
            """INSERT INTO transactions
               (id, amount, currency, created_at, skipped, processed_at)
               VALUES ('tx_old', -10.0, 'GBP', '2024-01-08 00:00:00', FALSE, NOW())"""
        )
        missing = get_missing_weeks()
        assert len(missing) > 0
        # All returned keys should match the week format
        for key in missing:
            assert re.match(r"^\d{4}-W\d{2}$", key)

    def test_already_sent_week_excluded(self, db):
        db.execute(
            """INSERT INTO transactions
               (id, amount, currency, created_at, skipped, processed_at)
               VALUES ('tx_old', -10.0, 'GBP', '2024-01-08 00:00:00', FALSE, NOW())"""
        )
        missing_before = get_missing_weeks()
        assert missing_before
        # Mark the first missing week as sent
        first = missing_before[0]
        db.execute(
            "INSERT INTO weekly_summaries (id, send_date, total_spend, total_income, net, sent_at) VALUES (1, ?, 0, 0, 0, NOW())",
            [first]
        )
        missing_after = get_missing_weeks()
        assert first not in missing_after


class TestGetMissingMonths:
    def test_empty_when_no_transactions(self, db):
        assert get_missing_months() == []

    def test_returns_missing_completed_months(self, db):
        db.execute(
            """INSERT INTO transactions
               (id, amount, currency, created_at, skipped, processed_at)
               VALUES ('tx_old', -10.0, 'GBP', '2024-01-15 00:00:00', FALSE, NOW())"""
        )
        missing = get_missing_months()
        assert len(missing) > 0
        for m in missing:
            assert re.match(r"^\d{4}-\d{2}$", m)

    def test_already_sent_month_excluded(self, db):
        db.execute(
            """INSERT INTO transactions
               (id, amount, currency, created_at, skipped, processed_at)
               VALUES ('tx_old', -10.0, 'GBP', '2024-01-15 00:00:00', FALSE, NOW())"""
        )
        missing_before = get_missing_months()
        assert missing_before
        first = missing_before[0]
        db.execute(
            "INSERT INTO monthly_summaries (id, send_date, total_spend, total_income, net, sent_at) VALUES (1, ?, 0, 0, 0, NOW())",
            [first]
        )
        assert first not in get_missing_months()


class TestGetWeekStats:
    def test_empty_week_returns_zeros(self, db):
        stats = get_week_stats("2026-W01")
        assert stats["total_spend"] == 0.0
        assert stats["total_income"] == 0.0
        assert stats["net"] == 0.0
        assert stats["spend_by_category"] == []
        assert stats["income_by_category"] == []

    def test_spend_counted_correctly(self, db):
        # Monday of W27 2026 is 2026-06-29
        db.execute(
            """INSERT INTO transactions
               (id, amount, currency, created_at, skipped, processed_at)
               VALUES ('tx_spend', -20.0, 'GBP', '2026-06-30 12:00:00', FALSE, NOW())"""
        )
        stats = get_week_stats("2026-W27")
        assert stats["total_spend"] == pytest.approx(20.0)
        assert stats["total_income"] == pytest.approx(0.0)

    def test_income_counted_correctly(self, db):
        db.execute(
            """INSERT INTO transactions
               (id, amount, currency, created_at, skipped, processed_at)
               VALUES ('tx_inc', 1000.0, 'GBP', '2026-06-30 12:00:00', FALSE, NOW())"""
        )
        stats = get_week_stats("2026-W27")
        assert stats["total_income"] == pytest.approx(1000.0)

    def test_net_is_income_minus_spend(self, db):
        db.execute(
            """INSERT INTO transactions
               (id, amount, currency, created_at, skipped, processed_at)
               VALUES ('tx_s', -30.0, 'GBP', '2026-06-30 12:00:00', FALSE, NOW()),
                      ('tx_i', 100.0, 'GBP', '2026-06-30 12:00:00', FALSE, NOW())"""
        )
        stats = get_week_stats("2026-W27")
        assert stats["net"] == pytest.approx(70.0)

    def test_transactions_outside_week_excluded(self, db):
        db.execute(
            """INSERT INTO transactions
               (id, amount, currency, created_at, skipped, processed_at)
               VALUES ('tx_outside', -50.0, 'GBP', '2026-07-06 12:00:00', FALSE, NOW())"""
        )
        stats = get_week_stats("2026-W27")
        assert stats["total_spend"] == 0.0

    def test_classified_spend_grouped_by_category(self, db):
        db.execute(
            """INSERT INTO transactions
               (id, amount, currency, created_at, skipped, llm_category, processed_at)
               VALUES ('tx_cat', -25.0, 'GBP', '2026-06-30 12:00:00', FALSE, 'Food & Drink', NOW())"""
        )
        stats = get_week_stats("2026-W27")
        cats = [c["category"] for c in stats["spend_by_category"]]
        assert "Food & Drink" in cats
