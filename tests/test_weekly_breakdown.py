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


PNG = b"\x89PNG\r\n\x1a\n" + b"not really an image"


def _record_telegram(monkeypatch):
    """Record what would be sent: texts and photos, in order."""
    sent = []
    monkeypatch.setattr(weekly_summary, "send_telegram", lambda text: sent.append(("text", text)) or True)
    monkeypatch.setattr(weekly_summary, "send_telegram_photo",
                        lambda png, caption="": sent.append(("photo", caption)) or True)
    monkeypatch.setattr(budget_messages, "send_charts", lambda *pngs: [sent.append(("photo", None)) for _ in pngs])
    return sent


def test_the_weekly_chart_is_a_real_chart_of_the_week(db):
    _week_with_a_deposit_back(db)
    png = weekly_summary.week_chart(weekly_summary.get_week_stats(WEEK))
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_summary_and_its_chart_go_as_one_message(db, monkeypatch):
    _week_with_a_deposit_back(db)
    sent = _record_telegram(monkeypatch)
    stats = weekly_summary.get_week_stats(WEEK)
    weekly_summary.send_weekly_report(WEEK, stats, weekly_summary.format_weekly_message(stats), PNG)
    assert [kind for kind, _ in sent] == ["photo"]
    assert "Spend by category" in sent[0][1]


def test_a_summary_too_long_for_a_caption_goes_first_then_the_chart(db, monkeypatch):
    _week_with_a_deposit_back(db)
    sent = _record_telegram(monkeypatch)
    stats = weekly_summary.get_week_stats(WEEK)
    long_message = "x" * 1100
    weekly_summary.send_weekly_report(WEEK, stats, long_message, PNG)
    assert [kind for kind, _ in sent] == ["text", "photo"]


def test_a_week_without_a_chart_is_just_the_text(db, monkeypatch):
    _week_with_a_deposit_back(db)
    sent = _record_telegram(monkeypatch)
    stats = weekly_summary.get_week_stats(WEEK)
    weekly_summary.send_weekly_report(WEEK, stats, weekly_summary.format_weekly_message(stats), None)
    assert [kind for kind, _ in sent] == ["text"]
