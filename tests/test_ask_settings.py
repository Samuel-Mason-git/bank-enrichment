"""The Ask settings: the model and effort the agent uses, the Telegram on/off and
chat timings, and the Ask page's on/off. Set on the Settings page's Ask tab."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import app_settings
from streamlit.testing.v1 import AppTest

import ask_settings as ASK
import ask_tg
import finance_agent as fa
from database_functions import get_setting
from tests.test_ask_telegram import Clock, FakeAgent, _chats, configured, sent  # noqa: F401  (configured is a fixture)

import asyncio

_DASHBOARD = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def test_the_defaults_are_the_cheap_model_and_half_an_hour(db):
    assert app_settings.get(ASK.MODEL) == "claude-haiku-5-5"
    assert app_settings.get(ASK.EFFORT) == "medium"
    assert app_settings.get(ASK.CHAT_MINUTES) == 30
    assert app_settings.get(ASK.PAGE_ON) is True and app_settings.get(ASK.TELEGRAM_ON) is True


def test_the_agent_uses_the_chosen_model_and_effort(db):
    app_settings.save(ASK.MODEL, "claude-sonnet-5-5")
    app_settings.save(ASK.EFFORT, "high")
    settings = fa.model_settings()
    assert settings["model"] == "claude-sonnet-5-5" and settings["effort"] == "high"


def test_an_unknown_model_is_refused(db):
    with pytest.raises(ValueError):
        app_settings.save(ASK.MODEL, "gpt-whatever")


def test_the_chat_minutes_must_be_in_range(db):
    with pytest.raises(ValueError):
        app_settings.save(ASK.CHAT_MINUTES, 1)
    app_settings.save(ASK.CHAT_MINUTES, 45)
    assert app_settings.get(ASK.CHAT_MINUTES) == 45


def test_telegram_switched_off_says_so_and_asks_nothing(db, configured):
    app_settings.save(ASK.TELEGRAM_ON, False)
    agent, bot = FakeAgent(), MagicMock()
    asyncio.run(_chats([agent]).answer(bot, 1, "how much?"))
    assert agent.asked == []
    assert sent(bot) == [ask_tg.OFF]


def test_a_shorter_chat_timeout_starts_afresh_sooner(db, configured):
    app_settings.save(ASK.CHAT_MINUTES, 5)
    first, second, clock, bot = FakeAgent(), FakeAgent(), Clock(), MagicMock()
    chats = _chats([first, second], clock)
    asyncio.run(chats.answer(bot, 1, "q1"))
    clock.now += 6 * 60                       # six minutes: past a five-minute timeout
    asyncio.run(chats.answer(bot, 1, "q2"))
    assert first.asked == ["q1"] and second.asked == ["q2"]


def test_the_default_timeout_still_carries_on_after_six_minutes(db, configured):
    first, second, clock, bot = FakeAgent(), FakeAgent(), Clock(), MagicMock()
    chats = _chats([first, second], clock)
    asyncio.run(chats.answer(bot, 1, "q1"))
    clock.now += 6 * 60
    asyncio.run(chats.answer(bot, 1, "q2"))
    assert first.asked == ["q1", "q2"] and second.asked == []


def test_a_budget_suggestion_expires_on_the_chosen_timeout(db, configured):
    app_settings.save(ASK.PROPOSAL_MINUTES, 5)
    clock = Clock()
    chats = _chats([FakeAgent()], clock)
    token = "tok"
    chats._proposals[token] = {"proposal": {"parent_id": 1, "amount": 50, "category": "Shopping"},
                               "chat_id": 1, "expires": clock() + app_settings.get(ASK.PROPOSAL_MINUTES) * 60}
    clock.now += 6 * 60
    assert chats.resolve(1, token, "set") == ask_tg.EXPIRED


def test_the_ask_page_can_be_switched_off(db, monkeypatch):
    monkeypatch.setenv("AGENT_DB_USER", "bank_enrichment_agent")
    monkeypatch.setenv("AGENT_DB_PASSWORD", "x")
    monkeypatch.setenv("CLAUDE_SECRET", "test")
    app_settings.save(ASK.PAGE_ON, False)
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.switch_page("views/ask.py").run()
    assert not at.exception
    assert any("switched off" in i.value for i in at.info)
    assert len(at.chat_input) == 0


def test_the_settings_ask_tab_saves_the_model(db):
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.switch_page("views/settings.py").run()
    assert not at.exception
    at.selectbox(key="ask_model").set_value("claude-sonnet-5-5").run()
    at.toggle(key="ask_tg_on").set_value(False).run()
    at.button(key="ask_settings_save").click().run()
    assert not at.exception
    assert get_setting("ask_model") == "claude-sonnet-5-5"
    assert get_setting("ask_telegram_on") is False
