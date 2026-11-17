"""Budget alerts you can set: the halfway and near thresholds, the pace rule, muting a category,
quiet hours, and a weekly total limit. Plus the Budgets tab that sets them."""
from datetime import datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

import app_settings
import budget_messages
import budgets as bg
import database_functions as dbf
from tests.local_helpers import insert_transaction
from tests.test_budgets import MID_OCT, _pid, shop, telegram  # noqa: F401  (shop and telegram are fixtures)
from tests.test_finance_agent import agent_db, money  # noqa: F401  (fixtures the shop fixture needs)

_DASHBOARD = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def _budget_shopping(shop, amount=100):
    bg.set_budget(_pid(shop, "Shopping"), amount)


# ── the halfway alert and the thresholds ─────────────────────────────────────

def test_halfway_is_warned_once_turned_on(shop, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: MID_OCT)
    _budget_shopping(shop, 100)                       # £60 spent of £100: 60%, past halfway
    assert "early" not in bg.alerts_wanted()                      # off by default
    bg.set_alerts_wanted(["early"])                               # on its own, so pace doesn't come first
    due = bg.alerts_due(bg.month_budgets(today=MID_OCT))
    assert [(s.category, kind) for s, kind in due] == [("Shopping", "early")]
    assert "halfway" in budget_messages.alert_text(due[0][0], "early", bg.month_budgets(today=MID_OCT))


def test_the_near_point_can_be_moved(shop, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: MID_OCT)
    _budget_shopping(shop, 100)                       # 60%
    bg.set_alerts_wanted(["early", "near", "over", "pace"])
    app_settings.save(bg.NEAR_PCT, 50)
    app_settings.save(bg.EARLY_PCT, 90)                # halfway warning moved out of the way
    kinds = [kind for _, kind in bg.alerts_due(bg.month_budgets(today=MID_OCT))]
    assert kinds == ["near"]


def test_a_stronger_alert_also_counts_the_weaker_ones(shop, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: MID_OCT)
    _budget_shopping(shop, 50)                        # £60 over £50: over budget
    mb = bg.month_budgets(today=MID_OCT)
    status = mb.statuses[0]
    bg.alert_sent(status, "over", mb.month)
    assert bg.alerts_due(mb) == []                    # over also covers near and halfway


# ── pace ─────────────────────────────────────────────────────────────────────

def test_the_pace_start_day_can_be_moved(shop, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: MID_OCT)
    _budget_shopping(shop, 1000)                      # nowhere near the limit, but heading past it by day 15
    app_settings.save(bg.PACE_FROM, 20)
    assert bg.month_budgets(today=MID_OCT).statuses[0].state == "ok"
    app_settings.save(bg.PACE_FROM, 7)
    assert bg.month_budgets(today=MID_OCT).statuses[0].state in ("pace", "ok")


# ── muting a category ────────────────────────────────────────────────────────

def test_a_muted_category_gets_no_alerts(shop, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: MID_OCT)
    _budget_shopping(shop, 100)
    bg.set_muted_parents([_pid(shop, "Shopping")])
    assert bg.alerts_due(bg.month_budgets(today=MID_OCT)) == []


# ── quiet hours ──────────────────────────────────────────────────────────────

def test_quiet_hours_wrap_past_midnight():
    th = {"quiet_from": 22, "quiet_to": 8}
    assert bg.in_quiet_hours(23, th) and bg.in_quiet_hours(0, th) and bg.in_quiet_hours(7, th)
    assert not bg.in_quiet_hours(8, th) and not bg.in_quiet_hours(12, th) and not bg.in_quiet_hours(21, th)


def test_the_same_start_and_end_means_no_quiet_hours():
    assert not bg.in_quiet_hours(3, {"quiet_from": 5, "quiet_to": 5})


def test_an_alert_held_overnight_goes_out_in_the_morning(shop, telegram, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: MID_OCT)
    _budget_shopping(shop, 100)
    night = datetime(2026, 10, 15, 23, 0, tzinfo=bg.LONDON)
    morning = datetime(2026, 10, 16, 8, 0, tzinfo=bg.LONDON)
    budget_messages.run(night)
    assert telegram["text"] == []                     # held
    budget_messages.run(morning)
    assert len(telegram["text"]) == 1                 # sent at the first run after


# ── the weekly total ─────────────────────────────────────────────────────────

def test_the_weekly_total_alert_fires_once_a_week(shop, telegram, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: MID_OCT)
    _budget_shopping(shop, 1000)                      # no category alerts: only the week
    insert_transaction(shop, "this_week", amount=-60, created_at="2026-10-13 12:00:00",
                       llm_category="Shopping", llm_subcategory="Clothing", merchant_name="Zara")
    app_settings.save(bg.WEEK_LIMIT, 50)
    bg.set_alerts_wanted(["week"])
    morning = datetime(2026, 10, 15, 9, 0, tzinfo=bg.LONDON)
    budget_messages.run(morning)
    budget_messages.run(morning)
    weekly = [t for t in telegram["text"] if "weekly limit" in t]
    assert len(weekly) == 1


def test_no_weekly_alert_when_the_limit_is_off(shop, telegram, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: MID_OCT)
    _budget_shopping(shop, 1000)
    budget_messages.run(datetime(2026, 10, 15, 9, 0, tzinfo=bg.LONDON))
    assert not any("weekly limit" in t for t in telegram["text"])


# ── the Budgets tab ──────────────────────────────────────────────────────────

def test_the_budgets_tab_has_every_setting(db):
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.switch_page("views/settings.py").run()
    assert not at.exception
    assert at.number_input(key="budget_early").value == 50
    assert at.number_input(key="budget_near").value == 80
    assert at.number_input(key="budget_pace_over").value == 110
    assert at.number_input(key="budget_week_limit").value == 0
    assert at.selectbox(key="budget_quiet_from").value == 22
    assert at.selectbox(key="budget_quiet_to").value == 8
    assert dbf.get_setting(bg.MUTED_SETTING) is None or dbf.get_setting(bg.MUTED_SETTING) == []
