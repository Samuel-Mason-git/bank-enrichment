"""The Ask page (src/dashboard/views/ask.py), with the agent faked: what it
shows before it's set up, an answer with its workings and cost, the starter
questions, follow-ups, a new chat, and every way a question can fail."""
import anthropic
import httpx
import pytest
import streamlit as st

import finance_agent as fa
from tests.test_dashboard_apptest import _app, _seed


@pytest.fixture(autouse=True)
def _clear_streamlit_cache():
    st.cache_data.clear()
    st.cache_resource.clear()
    yield


class FakeAgent:
    """Stands in for fa.FinanceAgent. raise_on_ask makes ask() fail instead."""
    raise_on_ask = None
    instances = []

    def __init__(self, client, *args, **kwargs):
        self.client = client
        self.cost_usd = 0.0
        self.daily_limit_usd = 0.30
        self.asked = []
        FakeAgent.instances.append(self)

    def ask(self, question, on_step=None):
        if FakeAgent.raise_on_ask:
            raise FakeAgent.raise_on_ask
        self.asked.append(question)
        steps = [fa.Step("get_totals", {"date_from": "2026-10-01", "date_to": "2026-10-31"},
                         '{"totals": {"spend": 57.0}}'),
                 fa.Step("run_sql", {"query": "SELECT 1"}, "Only a single SELECT", is_error=True)]
        for s in steps:
            if on_step:
                on_step(s)
        self.cost_usd += 0.0021
        charts = [{"kind": "bar", "title": "Spend by category", "metric": "spend", "group_by": "category",
                   "period": {"from": "2026-10-01", "to": "2026-10-31"},
                   "points": [{"label": "Food & Drink", "value": 50.0}, {"label": "Bills", "value": 7.0}]}]
        return fa.Answer(text=f"You spent £57.00 on: {question}", steps=steps, cost_usd=0.0021, stop_reason="end_turn",
                         charts=charts if "chart" in question else [])


@pytest.fixture
def ask(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("AGENT_DB_USER", "bank_enrichment_agent")
    monkeypatch.setenv("AGENT_DB_PASSWORD", "x")
    monkeypatch.setattr(fa, "FinanceAgent", FakeAgent)
    FakeAgent.raise_on_ask = None
    FakeAgent.instances = []
    return lambda: _app("ask")


def _text(at) -> str:
    return "\n".join(m.value for m in at.markdown) + "\n".join(c.value for c in at.caption)


def test_without_the_agent_login_the_page_explains_and_offers_nothing(db, monkeypatch):
    _seed(db)
    monkeypatch.delenv("AGENT_DB_USER", raising=False)
    at = _app("ask")
    assert not at.exception
    assert any("AGENT_DB_USER" in i.value for i in at.info)
    assert len(at.chat_input) == 0


def test_a_question_shows_the_answer_its_workings_and_cost(ask):
    at = ask()
    assert not at.exception
    at.chat_input[0].set_value("How much in October?").run()
    assert not at.exception
    text = _text(at)
    assert "You spent £57.00 on: How much in October?" in text
    assert "This answer \\$0.0021" in text and "daily limit" in text
    assert any("Show workings (2 steps)" in e.label for e in at.expander)
    codes = [c.value for c in at.code]
    assert any('"spend": 57.0' in c for c in codes) and "SELECT 1" in codes
    assert FakeAgent.instances[0].asked == ["How much in October?"]


def test_a_starter_question_is_asked_with_one_tap(ask):
    at = ask()
    starter = next(b for b in at.button if b.key == "ask_starter_2")
    starter.click().run()
    assert not at.exception
    assert FakeAgent.instances[0].asked == ["What are my subscriptions costing me a year?"]
    assert not any(b.key and b.key.startswith("ask_starter_") for b in at.button)   # gone once chatting


def test_follow_ups_go_to_the_same_agent_and_the_chat_stays_on_screen(ask):
    at = ask()
    at.chat_input[0].set_value("first").run()
    at.chat_input[0].set_value("second").run()
    assert len(FakeAgent.instances) == 1 and FakeAgent.instances[0].asked == ["first", "second"]
    assert "on: first" in _text(at) and "on: second" in _text(at)


def test_new_chat_starts_a_fresh_agent(ask):
    at = ask()
    at.chat_input[0].set_value("first").run()
    next(b for b in at.button if b.label == "New chat").click().run()
    assert not at.exception
    assert len(FakeAgent.instances) == 2
    assert "on: first" not in _text(at)


@pytest.mark.parametrize("error, shown", [
    (fa.DailyLimitReached("Today's limit of $0.30 for questions has been reached"), "Today's limit"),
    (fa.AgentNotConfigured("read-only database login isn't set up"), "read-only database login"),
    (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")), "couldn't be reached"),
    (RuntimeError("boom"), "Something went wrong"),
])
def test_a_failed_question_says_why_and_the_page_keeps_working(ask, error, shown):
    at = ask()
    FakeAgent.raise_on_ask = error
    at.chat_input[0].set_value("q").run()
    assert not at.exception
    assert any(shown in w.value for w in at.warning)
    FakeAgent.raise_on_ask = None
    at.chat_input[0].set_value("again").run()
    assert "on: again" in _text(at)


def test_the_daily_limit_is_set_on_the_settings_page(db):
    import database_functions as dbf
    _seed(db)
    at = _app("settings")
    assert not at.exception
    at.number_input(key="ask_daily_limit").set_value(1.5)
    next(b for b in at.button if b.key == "ask_daily_limit_save").click().run()
    assert not at.exception
    assert dbf.get_setting(fa.DAILY_LIMIT_SETTING) == 1.5
    assert fa.daily_limit() == 1.5



def test_a_chart_the_agent_asked_for_is_drawn_and_stays_on_screen(ask):
    at = ask()
    at.chat_input[0].set_value("chart my October spending").run()
    assert not at.exception
    assert len(at.get("plotly_chart")) == 1
    at.chat_input[0].set_value("and what about September?").run()
    assert not at.exception
    assert len(at.get("plotly_chart")) == 1          # the first still there, none added for the second


# ── saved chats ───────────────────────────────────────────────────────────────

def _saved(db):
    return db.execute("SELECT title, messages FROM saved_chats ORDER BY id").fetchall()


def _save(at, title=None):
    next(b for b in at.button if b.key == "ask_save").click().run()
    if title is not None:
        at.text_input[0].set_value(title)
    next(b for b in at.button if b.label == "Save").click().run()


def test_nothing_is_kept_unless_the_chat_is_saved(ask, db):
    at = ask()
    at.chat_input[0].set_value("chart my October").run()
    assert _saved(db) == []
    assert not any(e.label.startswith("📁 Saved chats") for e in at.expander)


def test_saving_keeps_what_was_on_screen(ask, db):
    at = ask()
    at.chat_input[0].set_value("chart my October").run()
    _save(at)
    assert not at.exception
    [(title, messages)] = _saved(db)
    assert title == "chart my October"                        # the first question, unless renamed
    assert [m["role"] for m in messages] == ["user", "assistant"]
    answer = messages[1]
    assert answer["text"].startswith("You spent £57.00") and answer["charts"][0]["title"] == "Spend by category"
    assert answer["steps"][0]["tool"] == "get_totals" and answer["steps"][1]["is_error"] is True
    assert any(e.label == "📁 Saved chats (1)" for e in at.expander)


def test_a_saved_chat_can_be_renamed_opened_and_read_back(ask, db):
    at = ask()
    at.chat_input[0].set_value("chart my October").run()
    _save(at, title="October breakdown")
    assert _saved(db)[0][0] == "October breakdown"
    next(b for b in at.button if b.label == "New chat").click().run()
    next(b for b in at.button if (b.key or "").startswith("ask_open_")).click().run()
    assert not at.exception
    assert at.subheader[0].value == "October breakdown"
    assert "You spent £57.00 on: chart my October" in "\n".join(m.value for m in at.markdown)
    assert len(at.get("plotly_chart")) == 1 and any("Show workings" in e.label for e in at.expander)
    # Read back, not carried on: none of the chat's own controls are drawn.
    assert not any(b.label in ("New chat", "Save chat") for b in at.button)
    next(b for b in at.button if b.key == "ask_back").click().run()
    assert any(b.key == "ask_starter_0" for b in at.button)   # back to a fresh chat


def test_deleting_a_saved_chat_asks_first(ask, db):
    at = ask()
    at.chat_input[0].set_value("q").run()
    _save(at)
    next(b for b in at.button if (b.key or "").startswith("ask_open_")).click().run()
    next(b for b in at.button if b.key == "ask_delete").click().run()
    next(b for b in at.button if b.key == "ask_delete_no").click().run()
    assert len(_saved(db)) == 1
    next(b for b in at.button if b.key == "ask_delete").click().run()
    next(b for b in at.button if b.key == "ask_delete_yes").click().run()
    assert not at.exception
    assert _saved(db) == [] and len(at.chat_input) == 1


def test_failed_turns_are_left_out_of_a_saved_chat(ask, db):
    at = ask()
    at.chat_input[0].set_value("first").run()
    FakeAgent.raise_on_ask = RuntimeError("boom")
    at.chat_input[0].set_value("second").run()
    FakeAgent.raise_on_ask = None
    _save(at)
    [(_, messages)] = _saved(db)
    assert [m.get("text", "")[:20] for m in messages if m["role"] == "assistant"] == ["You spent £57.00 on:"]
