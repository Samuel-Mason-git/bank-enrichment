"""/ask on Telegram: the Ask page's agent (src/pipeline/finance_agent.py), from
your phone.

    /ask how much have I spent this week?     a question
    /ask and the week before?                 a follow-up: the chat carries on
    /ask                                      start a fresh chat

A chat carries on for half an hour after its last question, then starts
afresh. Plain messages are left alone: they're still how you describe a
transaction. The agent reads through its own read-only login, shares the Ask
page's daily limit, and is told it's writing for a phone; its Markdown is
turned into the HTML Telegram shows. The question isn't logged and, like on
the Ask page, the answer's text isn't kept.

The webhook hands a question over and returns at once (Telegram retries a
webhook that takes too long); the answer is sent when it's ready, with the
"typing…" indicator showing meanwhile."""
import asyncio
import html
import logging
import os
import re
import time

import finance_agent as fa

log = logging.getLogger(__name__)

IDLE_SECONDS = 30 * 60
TYPING_EVERY = 4.0
# Telegram's limit is 4096 characters; the Markdown is cut before it becomes
# HTML, so a cut can never land inside a tag.
MAX_SOURCE_CHARS = 3500

HELP = ("🆕 New chat. Ask anything about your money, for example:\n\n"
        "<code>/ask how much have I spent this week?</code>\n"
        "<code>/ask what did I spend most on in October?</code>\n\n"
        "Follow up with another /ask within half an hour and it carries on from there.")
NOT_SET_UP = ("/ask isn't set up on the server: it needs AGENT_DB_USER, AGENT_DB_PASSWORD and CLAUDE_SECRET "
              "in config/.env (see SETUP.md).")
FAILED = "Something went wrong answering that. Try again in a moment."

_COMMAND = re.compile(r"^/ask(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def ask_text(text: str | None) -> str | None:
    """The question in an /ask message ("" for a bare /ask), or None when the
    message isn't one."""
    m = _COMMAND.match((text or "").strip())
    return None if m is None else (m.group(1) or "").strip()


def to_html(markdown: str) -> str:
    """The agent's Markdown as Telegram HTML: bold, code, bullets and headings
    kept; a table set out as fixed-width text; everything else escaped."""
    source = markdown.strip()
    if len(source) > MAX_SOURCE_CHARS:
        source = source[:MAX_SOURCE_CHARS].rstrip() + " …"
    out, table = [], []

    def flush():
        if table:
            widths = [max(len(r[i]) if i < len(r) else 0 for r in table) for i in range(max(map(len, table)))]
            lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in table]
            out.append("<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>")
            table.clear()

    for line in source.splitlines():
        if line.strip().startswith("|"):
            if not _TABLE_RULE.match(line):
                table.append([re.sub(r"\*\*(.+?)\*\*", r"\1", c.strip()) for c in line.strip().strip("|").split("|")])
            continue
        flush()
        t = html.escape(line, quote=False)
        t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t)
        t = re.sub(r"^(\s*)[-*]\s+", r"\1• ", t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        out.append(t)
    flush()
    return "\n".join(out)


def _default_agent():
    import anthropic
    return fa.FinanceAgent(anthropic.Anthropic(api_key=os.getenv("CLAUDE_SECRET")), channel="telegram")


async def _typing(bot, chat_id: int) -> None:
    try:
        await asyncio.to_thread(bot.send_chat_action, chat_id, "typing")
    except Exception:
        pass   # a missed indicator doesn't matter


async def _keep_typing(bot, chat_id: int) -> None:
    """The indicator fades after about five seconds: keep it showing."""
    while True:
        await asyncio.sleep(TYPING_EVERY)
        await _typing(bot, chat_id)


class AskChats:
    """Each chat's conversation with the agent, kept in memory: a server
    restart, or half an hour without a question, starts it afresh."""

    def __init__(self, make_agent=_default_agent, clock=time.monotonic):
        self._make_agent = make_agent
        self._clock = clock
        self._chats: dict[int, dict] = {}
        self._tasks: set[asyncio.Task] = set()

    def start(self, bot, chat_id: int, question: str) -> asyncio.Task:
        """Answer in the background, so the webhook can return straight away."""
        task = asyncio.create_task(self.answer(bot, chat_id, question))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def answer(self, bot, chat_id: int, question: str) -> None:
        if not question:
            self._chats.pop(chat_id, None)
            bot.send_message(chat_id, HELP)
            return
        if not (fa.is_configured() and os.getenv("CLAUDE_SECRET")):
            bot.send_message(chat_id, NOT_SET_UP)
            return
        chat = self._chats.get(chat_id)
        if chat is None or self._clock() - chat["last"] > IDLE_SECONDS:
            chat = {"agent": self._make_agent(), "lock": asyncio.Lock()}
            self._chats[chat_id] = chat
        chat["last"] = self._clock()

        # One question at a time per chat: a second /ask waits for the first.
        async with chat["lock"]:
            await _typing(bot, chat_id)   # straight away, before the agent starts
            typing = asyncio.create_task(_keep_typing(bot, chat_id))
            try:
                answer = await asyncio.to_thread(chat["agent"].ask, question)
            except fa.DailyLimitReached as e:
                reply = "⏸ " + html.escape(str(e), quote=False)
            except fa.AgentNotConfigured:
                reply = NOT_SET_UP
            except Exception as e:
                log.error(f"/ask failed: {type(e).__name__}: {e}")
                reply = FAILED
            else:
                log.info(f"Answered an /ask in {len(answer.steps)} step(s) for ${answer.cost_usd:.4f}")
                reply = to_html(answer.text) or FAILED
            finally:
                typing.cancel()
            chat["last"] = self._clock()
        bot.send_message(chat_id, reply)
