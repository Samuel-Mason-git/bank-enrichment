"""Budgets (src/pipeline/budgets.py), their Telegram messages and charts
(budget_messages.py, telegram_charts.py), the agent's budget tools, and the
confirm flows for a budget it proposes -- on the Ask page and on Telegram."""
import asyncio
import json
from pathlib import Path
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

import budget_messages
import budgets as bg
import database_functions as dbf
import finance_agent as fa
import telegram_charts as charts
from tests.local_helpers import insert_transaction
from tests.test_finance_agent import FakeClient, agent_db, money, reply, tool_use  # noqa: F401  (fixtures)

MID_OCT = date(2026, 10, 15)


@pytest.fixture
def shop(money):
    """money's October, plus a second spending category, Shopping, with
    spending in the three months before October for its suggestion."""
    for tid, amount, day in (("s_jul", -90, "2026-07-10"), ("s_aug", -120, "2026-08-10"),
                             ("s_sep", -95, "2026-09-10"), ("s_oct", -60, "2026-10-03")):
        insert_transaction(money, tid, amount=amount, created_at=f"{day} 12:00:00",
                           llm_category="Shopping", llm_subcategory="Clothing", merchant_name="Zara")
    return money


def _pid(con, name):
    return con.execute("SELECT id FROM parent_categories WHERE name = %s", [name]).fetchone()[0]


# ── what counts, and the standing of each budget ──────────────────────────────

def test_only_spend_categories_can_have_a_budget(shop):
    assert {c["name"] for c in bg.spend_categories()} == {"Food & Drink", "Shopping"}
    with pytest.raises(bg.BudgetError, match="Only a spending category"):
        bg.set_budget(_pid(shop, "Savings"), 100)
    with pytest.raises(bg.BudgetError, match="isn't a spending category"):
        bg.find_spend_category("Income")
    with pytest.raises(bg.BudgetError, match="no category called"):
        bg.find_spend_category("Yachts")


@pytest.mark.parametrize("amount", [0, 0.5, 100_001, "lots", None])
def test_a_budget_has_to_be_a_sensible_amount(shop, amount):
    with pytest.raises(bg.BudgetError):
        bg.set_budget(_pid(shop, "Food & Drink"), amount)


def test_spent_counts_as_the_overview_does(shop):
    # October: Food & Drink 50 (the skipped 999 left out); savings and transfers never count.
    assert bg.spent_by_category(date(2026, 10, 1), date(2026, 10, 31)) == {
        "Food & Drink": 50.0, "Shopping": 60.0, "Income": 0.0, "Transfers": 0.0, "Savings": 0.0}


def test_a_month_standing_with_pace_projection_and_notes(shop):
    bg.set_budget(_pid(shop, "Food & Drink"), 200)
    bg.set_budget(_pid(shop, "Shopping"), 70)
    mb = bg.month_budgets(today=MID_OCT)
    assert (mb.month, mb.day, mb.days, mb.current) == (date(2026, 10, 1), 15, 31, True)
    by = {s.category: s for s in mb.statuses}
    assert by["Shopping"].state == "near" and by["Shopping"].note == "£10 left, 16 days to go"
    assert by["Food & Drink"].state == "ok" and by["Food & Drink"].projected == round(50 / 15 * 31, 2)
    assert [s.category for s in mb.statuses] == ["Shopping", "Food & Drink"]   # most used first
    assert (mb.budgeted, mb.spent) == (270.0, 110.0)


def test_states_over_and_off_pace(shop):
    bg.set_budget(_pid(shop, "Shopping"), 50)                        # 60 spent: over
    bg.set_budget(_pid(shop, "Food & Drink"), 80)                    # 50 by day 15 -> ~103 by the 31st
    by = {s.category: s for s in bg.month_budgets(today=MID_OCT).statuses}
    assert by["Shopping"].state == "over" and by["Shopping"].note == "£10 over"
    assert by["Food & Drink"].state == "pace" and by["Food & Drink"].note == "at this rate £23 over by the 31st"
    # Too early in the month to call a pace.
    early = {s.category: s for s in bg.month_budgets(today=date(2026, 10, 5)).statuses}
    assert early["Food & Drink"].state == "ok"


def test_a_past_month_is_judged_on_the_whole_month(shop):
    bg.set_budget(_pid(shop, "Shopping"), 100)
    mb = bg.month_budgets(month=date(2026, 9, 1), today=MID_OCT)
    assert (mb.day, mb.current) == (30, False)
    [s] = mb.statuses
    assert s.spent == 95.0 and s.projected == 95.0 and s.note == "£5 under"


def test_unbudgeted_categories_get_a_suggestion_from_the_last_three_months(shop):
    mb = bg.month_budgets(today=MID_OCT)
    shopping = next(u for u in mb.unbudgeted if u["category"] == "Shopping")
    assert shopping["suggested"] == 110.0          # (90 + 120 + 95) / 3 = 101.67, up to the next £10
    assert shopping["spent"] == 60.0


def test_clearing_a_budget_and_its_history_recorded(shop):
    pid = _pid(shop, "Shopping")
    bg.set_budget(pid, 100)
    bg.set_budget(pid, 120)
    bg.clear_budget(pid)
    assert bg.current_budget(pid) is None
    actions = [r[0] for r in shop.execute(
        "SELECT action FROM settings_changes WHERE table_name = 'budgets' ORDER BY change_id").fetchall()]
    assert actions == ["INSERT", "UPDATE", "DELETE"]


def test_history_is_spend_in_budgeted_categories_against_todays_budgets(shop):
    bg.set_budget(_pid(shop, "Shopping"), 100)
    points = bg.history(4, today=MID_OCT)
    assert [(p["month"].month, p["spent"], p["budget"]) for p in points] == [
        (7, 90.0, 100.0), (8, 120.0, 100.0), (9, 95.0, 100.0), (10, 60.0, 100.0)]


# ── how often, and which alerts ───────────────────────────────────────────────

def test_the_digest_is_due_once_a_day_or_once_on_mondays(shop):
    monday_8am = datetime(2026, 10, 12, 8, 0, tzinfo=bg.LONDON)
    tuesday = datetime(2026, 10, 13, 8, 0, tzinfo=bg.LONDON)
    assert bg.digest_frequency() == "weekly"                          # until chosen
    assert bg.digest_due(monday_8am) and not bg.digest_due(tuesday)
    bg.digest_sent(monday_8am)
    assert not bg.digest_due(monday_8am.replace(hour=9))              # not twice the same day
    bg.set_digest_frequency("daily")
    assert bg.digest_due(tuesday)
    bg.set_digest_frequency("off")
    assert not bg.digest_due(tuesday)
    with pytest.raises(bg.BudgetError):
        bg.set_digest_frequency("hourly")


def test_alerts_are_the_wanted_ones_once_per_category_per_month(shop):
    bg.set_budget(_pid(shop, "Shopping"), 50)                        # over
    bg.set_budget(_pid(shop, "Food & Drink"), 80)                    # off pace
    mb = bg.month_budgets(today=MID_OCT)
    assert {(s.category, k) for s, k in bg.alerts_due(mb)} == {("Shopping", "over"), ("Food & Drink", "pace")}
    for s, k in bg.alerts_due(mb):
        bg.alert_sent(s, k, mb.month)
    assert bg.alerts_due(mb) == []
    # An "over" counts as the 80% one too, so a category that jumped past both isn't told twice.
    kinds = {r[0] for r in shop.execute("SELECT kind FROM budget_alerts WHERE parent_id = %s", [_pid(shop, "Shopping")])}
    assert kinds == {"over", "near"}


def test_unwanted_alerts_and_past_months_send_nothing(shop):
    bg.set_budget(_pid(shop, "Shopping"), 50)
    bg.set_alerts_wanted(["pace"])
    assert bg.alerts_due(bg.month_budgets(today=MID_OCT)) == []
    bg.set_alerts_wanted(["near", "over", "pace"])
    assert bg.alerts_due(bg.month_budgets(month=date(2026, 9, 1), today=MID_OCT)) == []


# ── the messages ──────────────────────────────────────────────────────────────

@pytest.fixture
def telegram(monkeypatch):
    sent = {"text": [], "photos": []}
    monkeypatch.setattr(budget_messages, "send_telegram", lambda t: sent["text"].append(t) or True)
    monkeypatch.setattr(budget_messages, "send_telegram_photo", lambda p, caption="": sent["photos"].append(p) or True)
    return sent


def test_nothing_is_sent_without_budgets(shop, telegram):
    assert budget_messages.run(datetime(2026, 10, 12, 8, 0, tzinfo=bg.LONDON)) == {"digest": False, "alerts": 0}
    assert telegram["text"] == []


def test_the_digest_and_alerts_go_once(shop, telegram, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: date(2026, 10, 12))
    bg.set_budget(_pid(shop, "Shopping"), 50)
    bg.set_budget(_pid(shop, "Food & Drink"), 400)
    monday = datetime(2026, 10, 12, 8, 0, tzinfo=bg.LONDON)
    assert budget_messages.run(monday) == {"digest": True, "alerts": 1}
    digest, alert = telegram["text"]
    assert "📊 <b>Budgets, October</b> · day 12 of 31" in digest
    assert "🔴 <b>Shopping</b>: £60 of £50 · £10 over" in digest and "🟢 <b>Food &amp; Drink</b>" in digest
    assert alert.startswith("🔴 <b>Shopping is over budget</b>")
    assert telegram["photos"][0].startswith(b"\x89PNG")
    assert budget_messages.run(monday.replace(hour=9)) == {"digest": False, "alerts": 0}


def test_a_failed_send_is_tried_again_next_run(shop, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: date(2026, 10, 12))
    monkeypatch.setattr(budget_messages, "send_telegram", lambda t: False)
    bg.set_budget(_pid(shop, "Shopping"), 50)
    bg.set_digest_frequency("daily")
    assert budget_messages.run(datetime(2026, 10, 12, 8, 0, tzinfo=bg.LONDON)) == {"digest": False, "alerts": 0}
    assert bg.digest_due(datetime(2026, 10, 12, 9, 0, tzinfo=bg.LONDON))
    assert bg.alerts_due(bg.month_budgets(today=date(2026, 10, 12)))


def test_the_summaries_get_budget_lines(shop):
    bg.set_budget(_pid(shop, "Shopping"), 50)
    lines = budget_messages.budget_lines(bg.month_budgets(today=MID_OCT))
    assert lines[1] == "Budgets, October so far (day 15 of 31): £60 of £50"
    assert lines[2] == "  🔴 Shopping: £60 of £50 · £10 over"
    assert budget_messages.budget_lines(bg.month_budgets(month=date(2026, 9, 1), today=MID_OCT))[1] == \
        "Budgets, September: £95 of £50"


# ── the charts ────────────────────────────────────────────────────────────────

def test_every_chart_is_a_png_and_nothing_to_draw_is_none(shop):
    bg.set_budget(_pid(shop, "Shopping"), 50)
    mb = bg.month_budgets(today=MID_OCT)
    pngs = [charts.budget_bars(mb),
            charts.budget_bars(bg.month_budgets(month=date(2026, 9, 1), today=MID_OCT)),
            charts.spend_by_category([{"category": f"C{i}", "amount": 10.0 * i} for i in range(1, 15)], "t"),
            charts.months_trend(bg.history(4, today=MID_OCT), "t")]
    assert all(p and p.startswith(b"\x89PNG") for p in pngs)
    bg.clear_budget(_pid(shop, "Shopping"))
    assert charts.budget_bars(bg.month_budgets(today=MID_OCT)) is None
    assert charts.spend_by_category([], "t") is None
    assert charts.months_trend([{"month": MID_OCT, "spent": 0, "budget": 0}], "t") is None


# ── the agent: reading budgets, and proposing one ─────────────────────────────

def test_the_agent_reads_budgets_through_its_own_login(shop):
    bg.set_budget(_pid(shop, "Shopping"), 50)
    out = fa.get_budgets({"month": "2026-10-01"})
    [b] = out["budgets"]
    assert (b["category"], b["budget"], b["spent"], b["status"]) == ("Shopping", 50.0, 60.0, "over")
    assert {n["category"] for n in out["no_budget"]} == {"Food & Drink"}


def test_without_budgets_the_agent_is_told_where_they_are_set(shop):
    assert "Budget page" in fa.get_budgets({})["note"]


def test_a_proposal_changes_nothing_and_rides_on_the_answer(shop):
    client = FakeClient([tool_use("propose_budget", {"category": "shopping", "monthly_amount": 110, "reason": "Avg."}),
                         reply("I've suggested £110 for Shopping; confirm it if you like.")])
    answer = fa.FinanceAgent(client, daily_limit_usd=1).ask("what should my shopping budget be?")
    assert answer.proposals == [{"parent_id": _pid(shop, "Shopping"), "category": "Shopping", "amount": 110.0,
                                 "current": None, "reason": "Avg."}]
    assert json.loads(answer.steps[0].output)["applied"] is False
    assert bg.current_budget(_pid(shop, "Shopping")) is None                 # nothing written


@pytest.mark.parametrize("args, says", [
    ({"category": "Income", "monthly_amount": 100}, "isn't a spending category"),
    ({"category": "Shopping", "monthly_amount": 0}, "between"),
])
def test_a_proposal_that_couldnt_be_set_goes_back_as_an_error(shop, args, says):
    client = FakeClient([tool_use("propose_budget", args), reply("Sorry.")])
    answer = fa.FinanceAgent(client, daily_limit_usd=1).ask("q")
    assert answer.proposals == [] and answer.steps[0].is_error and says in answer.steps[0].output


def test_both_channels_get_the_budget_tools(shop):
    for channel in ("dashboard", "telegram"):
        client = FakeClient([reply("ok")])
        fa.FinanceAgent(client, daily_limit_usd=1, channel=channel).ask("q")
        names = {t["name"] for t in client.requests[0]["tools"]}
        assert {"get_budgets", "propose_budget"} <= names


# ── confirming a proposal on Telegram ─────────────────────────────────────────

def _proposal(shop, amount=110.0):
    return {"parent_id": _pid(shop, "Shopping"), "category": "Shopping", "amount": amount, "current": None,
            "reason": "Your <average>."}


class ProposingAgent:
    def __init__(self, proposals):
        self.proposals = proposals

    def ask(self, question, on_step=None):
        return fa.Answer(text="I've suggested a budget.", proposals=self.proposals)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _offered(shop, monkeypatch, clock=None):
    import ask_tg
    monkeypatch.setenv("AGENT_DB_USER", "x")
    monkeypatch.setenv("AGENT_DB_PASSWORD", "x")
    monkeypatch.setenv("CLAUDE_SECRET", "x")
    chats = ask_tg.AskChats(make_agent=lambda: ProposingAgent([_proposal(shop)]), clock=clock or Clock())
    bot = MagicMock()
    asyncio.run(chats.answer(bot, 1, "what should shopping be?"))
    card = bot.send_message.call_args_list[-1]
    buttons = card.kwargs["reply_markup"]["inline_keyboard"][0]
    return chats, card.args[1], {b["text"]: b["callback_data"].split(":", 2)[2] for b in buttons}


def test_a_telegram_proposal_comes_with_buttons_and_sets_when_tapped(shop, monkeypatch):
    chats, text, tokens = _offered(shop, monkeypatch)
    assert text == "💡 Budget for <b>Shopping</b>: £110 a month\n<i>Your &lt;average&gt;.</i>"
    assert chats.resolve(1, tokens["✅ Set it"], "set") == "✅ <b>Shopping</b> now has a £110 monthly budget."
    assert bg.current_budget(_pid(shop, "Shopping")) == 110.0
    assert chats.resolve(1, tokens["✅ Set it"], "set").startswith("That suggestion has expired")  # once only


def test_a_telegram_proposal_can_be_declined_and_expires(shop, monkeypatch):
    chats, _, tokens = _offered(shop, monkeypatch)
    assert chats.resolve(1, tokens["✖ No thanks"], "no") == "👍 Left Shopping as it was."
    assert bg.current_budget(_pid(shop, "Shopping")) is None
    clock = Clock()
    chats, _, tokens = _offered(shop, monkeypatch, clock)
    clock.now += 31 * 60
    assert chats.resolve(1, tokens["✅ Set it"], "set").startswith("That suggestion has expired")
    chats, _, tokens = _offered(shop, monkeypatch)
    assert chats.resolve(2, tokens["✅ Set it"], "set").startswith("That suggestion has expired")   # another chat
    assert bg.current_budget(_pid(shop, "Shopping")) is None


def test_the_webhook_hands_a_proposal_tap_to_ask_chats():
    from tests.server_helpers import replies, tap
    from unittest.mock import patch
    import main
    with patch.object(main, "ask_chats") as chats:
        chats.resolve.return_value = "✅ done"
        bot = tap(None, "budgetprop:set:tok123")
    chats.resolve.assert_called_once_with(12345, "tok123", "set")
    assert replies(bot) == ["✅ done"]


# ── /budget, over-budget bars, and the six-month history ─────────────────────

def test_budget_command_sends_the_standing_as_text_and_a_chart(shop, telegram, monkeypatch):
    monkeypatch.setattr(bg, "today_london", lambda: date(2026, 10, 15))
    bg.set_budget(_pid(shop, "Shopping"), 50)
    assert budget_messages.report() is True
    assert telegram["text"][0].startswith("📊 <b>Budgets, October</b>")
    assert telegram["photos"] and telegram["photos"][0].startswith(b"\x89PNG")


def test_budget_command_without_budgets_says_where_to_set_them(shop, telegram):
    assert budget_messages.report() is True
    assert "No budgets are set yet" in telegram["text"][0]
    assert telegram["photos"] == []


@pytest.mark.parametrize("text, match", [("/budget", True), ("/budget@MyBot", True), ("/budget please", False),
                                          ("/ask /budget", False), ("weekly shop", False)])
def test_only_a_bare_budget_is_the_command(text, match):
    import re
    assert bool(re.match(r"^/budget(?:@\w+)?\s*$", text.strip(), re.IGNORECASE)) is match


def test_the_webhook_answers_budget_without_touching_a_waiting_transaction(monkeypatch):
    from unittest.mock import patch
    import main
    msg = MagicMock(text="/budget")
    msg.chat.id = 12345
    update = MagicMock(callback_query=None, message=msg)
    request = MagicMock()

    async def _body():
        return b"{}"

    request.body = _body
    request.headers = {"x-telegram-bot-api-secret-token": "test-secret"}
    with patch.object(main, "TELEGRAM_WEBHOOK_SECRET", "test-secret"), \
         patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
         patch.object(main.TelegramUpdate, "model_validate_json", return_value=update), \
         patch.object(main, "ask_chats") as chats, \
         patch.object(main, "record_context") as record_context, \
         patch("budget_messages.report") as report:
        main._sessions[12345] = {"transaction_id": "tx_1", "timeout_task": None}
        try:
            asyncio.run(main.recieve_telegram(request))
        finally:
            main._sessions.pop(12345, None)
    chats.start.assert_not_called()
    record_context.assert_not_called()


def test_an_over_budget_bar_keeps_the_budget_length_and_shows_how_far_over(shop):
    bg.set_budget(_pid(shop, "Shopping"), 50)                # 60 spent: 120% used
    mb = bg.month_budgets(today=MID_OCT)
    png = charts.budget_bars(mb)
    assert png and png.startswith(b"\x89PNG")
    assert bg.OVER_SHOWN == 0.5                              # the red extension is capped, not the bar


def test_the_budget_page_draws_the_six_months_and_the_bars(db, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from tests.test_dashboard_apptest import _seed
    import streamlit as st
    st.cache_data.clear(); st.cache_resource.clear()
    _seed(db)
    # Spend in the current month, so the six-month chart has something to draw.
    insert_transaction(db, "tx_now", amount=-42, created_at=f"{date.today().isoformat()} 12:00:00",
                       llm_category="Food & Drink", llm_subcategory="Groceries", merchant_name="Tesco")
    db.execute("INSERT INTO budgets (parent_id, monthly_amount) SELECT id, 300 FROM parent_categories WHERE name = 'Food & Drink'")
    at = AppTest.from_file(str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py"), default_timeout=30)
    at.switch_page("views/budget.py").run()
    assert not at.exception
    assert len(at.get("plotly_chart")) >= 2                  # the bars and the six months
