"""The /help message: what the bot understands, sent to you on Telegram."""
import re

HELP = (
    "<b>Commands</b>\n"
    "/ask &lt;question&gt; — ask about your money, e.g. <code>/ask how much did I spend this week?</code>\n"
    "/ask on its own — start a fresh chat\n"
    "/budget — this month's budgets, as text and a chart\n"
    "/health — is the app working: server, database, last payment and last run\n"
    "/help — this message\n\n"
    "<b>Anything else</b>\n"
    "A plain message answers the payment I've just asked you about: a sentence of context, "
    "or Skip. If you tap a category button, that's the context instead.\n\n"
    "Budget messages and alerts, and the Ask settings, are on the dashboard's Settings page."
)

HELP_COMMAND = re.compile(r"^/help(?:@\w+)?\s*$", re.IGNORECASE)


def is_help(text: str | None) -> bool:
    return bool(HELP_COMMAND.match((text or "").strip()))
