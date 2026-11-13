"""agent_eval.py: the answer key it builds from the data, how it grades an
answer, and that eval calls stay out of the Ask page's daily limit."""
from datetime import date

import pytest

import agent_eval as ae
import finance_agent as fa
from tests.test_finance_agent import FakeClient, agent_db, money, reply  # noqa: F401  (fixtures)


# ── grading ───────────────────────────────────────────────────────────────────

def test_money_is_read_in_the_ways_answers_write_it():
    assert ae.money_in("£3,841.53, up from £57 and £1.2k; not 42 or $9") == [3841.53, 57.0, 1200.0]


def test_an_answer_passes_with_every_figure_name_and_count():
    q = ae.Question("q", amounts=[3841.53], names=["Transport"], counts=[74])
    assert ae.grade(q, "You spent **£3,841.53**, most on transport, in 74 payments.") == []
    assert ae.grade(q, "About £3,842 on Transport over 74 payments.") == []          # rounded to the pound


def test_an_answer_fails_and_says_what_is_missing():
    q = ae.Question("q", amounts=[3841.53], names=["Transport"], counts=[74])
    problems = ae.grade(q, "You spent £3,900.00 on food over 740 payments, £74.00 in fees.")
    assert len(problems) == 3
    assert "no £3,841.53" in problems[0] and "£3,900.00" in problems[0]
    assert "Transport" in problems[1] and "count of 74" in problems[2]


# ── the answer key ────────────────────────────────────────────────────────────

def test_the_answer_key_counts_money_the_way_the_dashboard_does(money):
    qs = {q.text: q for q in ae.build_questions(date(2026, 11, 13))}
    assert qs["How much did I spend in October 2026?"].amounts == [57.0]          # not the pot or transfers
    assert qs["What was my total income in October 2026?"].amounts == [2000.0]
    assert qs["How did my spending in October 2026 compare with September 2026?"].amounts == [57.0, 50.0]
    assert qs["What was my net for October 2026: income minus spending?"].amounts == [1943.0]
    top = qs["Which category did I spend the most on in October 2026, and how much?"]
    assert top.amounts == [50.0] and top.names == ["Food & Drink"]
    assert qs["How much did I put into savings in October 2026, after any withdrawals?"].amounts == [200.0]
    assert qs["What was my biggest single purchase in October 2026?"].amounts == [30.0]
    assert qs["What do my active subscriptions cost me a year?"].amounts == [pytest.approx((10.99 + 1) * 12)]


# ── eval calls and the daily limit ────────────────────────────────────────────

def test_eval_calls_are_labelled_and_ignore_the_daily_limit(money):
    money.execute("INSERT INTO llm_calls (started_at, job, model, cost_usd) VALUES (now(), 'finance_agent', 'm', 5)")
    agent = fa.FinanceAgent(FakeClient([reply("£57.00")]), daily_limit_usd=0.30, job="agent_eval")
    assert agent.ask("q").text == "£57.00"                                         # not stopped by the limit
    jobs = [r[0] for r in money.execute("SELECT job FROM llm_calls ORDER BY call_id").fetchall()]
    assert jobs == ["finance_agent", "agent_eval"]
    assert fa.spent_today() == pytest.approx(5.0)                                   # and not counted in it
