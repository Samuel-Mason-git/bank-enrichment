"""/ask on Telegram (src/server/ask_tg.py and its line in main.py): which
messages are questions, the reply's HTML, a chat carrying on and starting
afresh, every way a question can fail -- and that plain messages still go to
describing a transaction."""
import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

import ask_tg
import database_functions
import finance_agent as fa


# ── which messages are questions ──────────────────────────────────────────────

@pytest.mark.parametrize("text, question", [
    ("/ask how much this week?", "how much this week?"),
    ("/ask@MyMoneyBot how much?", "how much?"),
    ("  /ASK   and last week?  ", "and last week?"),
    ("/ask line one\nline two", "line one\nline two"),
    ("/ask", ""),
    ("/ask@MyMoneyBot", ""),
    ("/asking for a friend", None),
    ("weekly shop", None),
    ("", None),
    (None, None),
])
def test_ask_text(text, question):
    assert ask_tg.ask_text(text) == question


# ── the reply's HTML ──────────────────────────────────────────────────────────

def test_markdown_becomes_telegram_html_and_everything_else_is_escaped():
    html = ask_tg.to_html("## October\nYou spent **£3,841.53** <less> than `Sep` & more.\n- Rent: £943\n* Food: £594")
    assert html == ("<b>October</b>\nYou spent <b>£3,841.53</b> &lt;less&gt; than <code>Sep</code> &amp; more.\n"
                    "• Rent: £943\n• Food: £594")


def test_a_table_becomes_fixed_width_text():
    html = ask_tg.to_html("Compared:\n\n| Category | Sep | Oct |\n|---|---:|---|\n| **Bills** | £4,641 | £1,208 |\n| Food & Drink | £417 | £594 |\n\nDone.")
    assert "<pre>Category      Sep     Oct\nBills         £4,641  £1,208\nFood &amp; Drink  £417    £594</pre>" in html
    assert "|" not in html and html.endswith("Done.")


def test_a_long_answer_is_cut_before_it_becomes_html():
    html = ask_tg.to_html("**" + "x" * 5000 + "**")
    assert len(html) < 4096 and "<b>" not in html          # the cut came before the closing **, so no half tag
    assert html.endswith("…")


# ── answering ─────────────────────────────────────────────────────────────────

class FakeAgent:
    def __init__(self, fail=None):
        self.asked, self.fail = [], fail

    def ask(self, question, on_step=None):
        self.asked.append(question)
        if self.fail:
            raise self.fail
        return fa.Answer(text=f"You spent **£57.00** ({question})", steps=[], cost_usd=0.001)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("AGENT_DB_USER", "bank_enrichment_agent")
    monkeypatch.setenv("AGENT_DB_PASSWORD", "x")
    monkeypatch.setenv("CLAUDE_SECRET", "test")


def _chats(agents, clock=None):
    made = iter(agents)
    return ask_tg.AskChats(make_agent=lambda: next(made), clock=clock or Clock())


def sent(bot) -> list[str]:
    return [c.args[1] for c in bot.send_message.call_args_list]


def test_a_question_is_answered_in_html(configured):
    agent, bot = FakeAgent(), MagicMock()
    asyncio.run(_chats([agent]).answer(bot, 1, "how much?"))
    assert agent.asked == ["how much?"]
    assert sent(bot) == ["You spent <b>£57.00</b> (how much?)"]
    bot.send_chat_action.assert_called_with(1, "typing")


def test_a_follow_up_carries_on_but_half_an_hour_later_starts_afresh(configured):
    first, second, clock, bot = FakeAgent(), FakeAgent(), Clock(), MagicMock()
    chats = _chats([first, second], clock)
    asyncio.run(chats.answer(bot, 1, "one"))
    clock.now += 60
    asyncio.run(chats.answer(bot, 1, "two"))
    clock.now += ask_tg.IDLE_SECONDS + 1
    asyncio.run(chats.answer(bot, 1, "three"))
    assert first.asked == ["one", "two"] and second.asked == ["three"]


def test_a_bare_ask_starts_a_fresh_chat_and_explains(configured):
    first, second, bot = FakeAgent(), FakeAgent(), MagicMock()
    chats = _chats([first, second])
    asyncio.run(chats.answer(bot, 1, "one"))
    asyncio.run(chats.answer(bot, 1, ""))
    asyncio.run(chats.answer(bot, 1, "two"))
    assert sent(bot)[1] == ask_tg.HELP
    assert first.asked == ["one"] and second.asked == ["two"]


def test_each_chat_has_its_own_conversation(configured):
    a, b, bot = FakeAgent(), FakeAgent(), MagicMock()
    chats = _chats([a, b])
    asyncio.run(chats.answer(bot, 1, "mine"))
    asyncio.run(chats.answer(bot, 2, "theirs"))
    assert a.asked == ["mine"] and b.asked == ["theirs"]


def test_not_set_up_says_so_and_asks_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_DB_USER", raising=False)
    agent, bot = FakeAgent(), MagicMock()
    asyncio.run(_chats([agent]).answer(bot, 1, "q"))
    assert sent(bot) == [ask_tg.NOT_SET_UP] and agent.asked == []


@pytest.mark.parametrize("error, reply", [
    (fa.DailyLimitReached("Today's limit of $0.30 <for questions> has been reached"),
     "⏸ Today's limit of $0.30 &lt;for questions&gt; has been reached"),
    (fa.AgentNotConfigured("no login"), ask_tg.NOT_SET_UP),
    (RuntimeError("API down"), ask_tg.FAILED),
])
def test_a_failed_question_says_why(configured, error, reply):
    bot = MagicMock()
    asyncio.run(_chats([FakeAgent(fail=error)]).answer(bot, 1, "q"))
    assert sent(bot) == [reply]


def test_the_question_is_never_logged(configured, caplog):
    caplog.set_level(logging.INFO)
    secret = "how much did I spend at the clinic"
    asyncio.run(_chats([FakeAgent()]).answer(MagicMock(), 1, secret))
    asyncio.run(_chats([FakeAgent(fail=RuntimeError("down"))]).answer(MagicMock(), 1, secret))
    assert secret not in caplog.text and "Answered an /ask" in caplog.text


def test_start_answers_in_the_background(configured):
    agent, bot = FakeAgent(), MagicMock()
    chats = _chats([agent])

    async def go():
        task = chats.start(bot, 1, "q")
        assert not task.done()                 # handed over: the webhook returns before the answer
        await task

    asyncio.run(go())
    assert sent(bot) == ["You spent <b>£57.00</b> (q)"]


# ── the webhook ───────────────────────────────────────────────────────────────

def _message(text: str, chat_id: int = 12345):
    import main
    msg = MagicMock(text=text)
    msg.chat.id = chat_id
    update = MagicMock(callback_query=None, message=msg)
    request = MagicMock()

    async def _body():
        return b"{}"

    request.body = _body
    request.headers = {"x-telegram-bot-api-secret-token": "test-secret"}
    bot, chats = MagicMock(), MagicMock()
    with patch.object(main, "bot", bot), patch.object(main, "ask_chats", chats), \
         patch.object(main, "TELEGRAM_WEBHOOK_SECRET", "test-secret"), \
         patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}), \
         patch.object(main.TelegramUpdate, "model_validate_json", return_value=update), \
         patch.object(main, "record_context") as record_context:
        main._sessions[chat_id] = {"transaction_id": "tx_1", "timeout_task": None}
        try:
            asyncio.run(main.recieve_telegram(request))
        finally:
            main._sessions.pop(chat_id, None)
    return bot, chats, record_context


def test_an_ask_message_goes_to_the_agent_not_to_the_waiting_transaction():
    bot, chats, record_context = _message("/ask how much this week?")
    chats.start.assert_called_once_with(bot, 12345, "how much this week?")
    record_context.assert_not_called()


def test_a_plain_message_still_describes_the_waiting_transaction():
    with patch("main.get_con"):
        bot, chats, record_context = _message("weekly shop")
    chats.start.assert_not_called()
    assert record_context.call_args.args[1:3] == ("tx_1", "weekly shop")


def test_someone_else_asking_gets_nothing():
    bot, chats, record_context = _message("/ask how much does he earn?", chat_id=666)
    chats.start.assert_not_called()


# ── the server's connection, shared ───────────────────────────────────────────

def test_the_pipeline_can_use_the_servers_connection():
    con = object()
    database_functions.use_connection_from(lambda: con)
    assert database_functions.get_con() is con


def test_the_telegram_prompt_asks_for_phone_sized_replies():
    from datetime import date
    assert "Telegram" in fa.system_prompt(date(2026, 11, 13), "telegram")
    assert "Telegram" not in fa.system_prompt(date(2026, 11, 13))
