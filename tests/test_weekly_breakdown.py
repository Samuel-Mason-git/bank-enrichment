"""The weekly summary: its category breakdown adds up to its total, a deposit coming back doesn't
cancel a bill, and the weekly message has no budget block and sends one chart (spend), not budget ones."""
from datetime import date

import budget_messages
import weekly_summary
from tests.local_helpers import ensure_category, insert_transaction, label

WEEK = weekly_summary._week_key(date(2026, 11, 16))


def _week_with_a_deposit_back(db):
    ensure_category(db, "Bills & Utilities", "Electricity")
    ensure_category(db, "Bills & Utilities", "Holding Deposit")
    insert_transaction(db, "power", amount=-101.04, description="OCTOPUS", merchant_name="Octopus Energy",
                       created_at="2026-11-16 09:00:00")
    label(db, "power", "Bills & Utilities", "Electricity")
    insert_transaction(db, "deposit", amount=1404.43, description="DEPOSIT PROTECTION",
                       counterparty_name="Deposit Protection Service", created_at="2026-11-20 09:00:00")
    label(db, "deposit", "Bills & Utilities", "Holding Deposit")


def test_a_deposit_coming_back_does_not_cancel_the_bill_it_was_for(db):
    _week_with_a_deposit_back(db)
    stats = weekly_summary.get_week_stats(WEEK)
    assert stats["total_spend"] == 101.04
    by_category = {r["category"]: r["amount"] for r in stats["spend_by_category"]}
    assert by_category == {"Bills & Utilities": 101.04}


def test_the_categories_add_up_to_the_total(db):
    _week_with_a_deposit_back(db)
    stats = weekly_summary.get_week_stats(WEEK)
    assert round(sum(r["amount"] for r in stats["spend_by_category"]), 2) == stats["total_spend"]


def test_the_weekly_message_has_no_budget_block(db):
    _week_with_a_deposit_back(db)
    message = weekly_summary.format_weekly_message(weekly_summary.get_week_stats(WEEK))
    assert "Budgets" not in message
    assert "Spend by category" in message


def test_the_newest_week_sends_one_spend_chart_and_no_budget_charts(db, monkeypatch):
    _week_with_a_deposit_back(db)
    sent = []
    monkeypatch.setattr(budget_messages, "send_charts", lambda *pngs: sent.extend(pngs) or len(pngs))
    weekly_summary.send_week_charts(weekly_summary.get_week_stats(WEEK))
    assert len(sent) == 1 and sent[0] and sent[0][:8] == b"\x89PNG\r\n\x1a\n"
