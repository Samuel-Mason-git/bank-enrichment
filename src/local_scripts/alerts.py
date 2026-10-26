"""Tell the user, on Telegram, when the pipeline can't do its job.

The classifier catches every Anthropic error and carries on -- a failed pass
returns nothing and its transactions simply stay unclassified -- which keeps one
bad call from killing a run, but it also means running out of credit produces
nothing except error lines in a log file. That is easy to miss, and the
symptom (transactions quietly not being classified) looks like nothing at all.

This sends to Telegram directly, with the same bot the server uses (TELEGRAM_API
and TELEGRAM_CHAT_ID in config/.env). Alerts carry a kind, a fix, and counts --
never transaction text. The exception classes, not their messages, are what get
reported: a JSON parse error's message quotes the model's output, which can
contain transaction details.

Nothing here may ever raise or slow the pipeline down: an alert that fails is
logged and dropped, and each kind is sent at most once per run, so a run that
fails 40 calls in a row sends one message, not 40.
"""
import html
import logging
import os
import threading
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

log = logging.getLogger(__name__)


def send_telegram(text: str) -> bool:
    """One HTML message to you on Telegram. Never raises: a message that can't
    be sent is logged and dropped. True if Telegram accepted it."""
    token, chat_id = os.getenv("TELEGRAM_API"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_API or TELEGRAM_CHAT_ID isn't set -- Telegram message not sent")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15,
        )
    except Exception as e:
        log.warning(f"Could not reach Telegram: {type(e).__name__}")
        return False
    if not response.ok:
        log.warning(f"Telegram rejected a message: HTTP {response.status_code}")
        return False
    return True

_sent: set[str] = set()
_lock = threading.Lock()


def classify_error(e: Exception) -> tuple[str, str, str]:
    """(kind, title, what to do) for an exception out of an Anthropic call."""
    if isinstance(e, anthropic.BadRequestError) and "credit balance" in str(e):
        return (
            "credit", "Anthropic credit balance too low",
            "Classification is paused until you top up (console.anthropic.com → Billing). "
            "Nothing is lost — transactions stay unclassified and are retried on the next run.",
        )
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return (
            "auth", "Anthropic API key rejected",
            "Check CLAUDE_SECRET in config/.env. Nothing is classified until it works; "
            "transactions are retried on the next run.",
        )
    detail = type(e).__name__
    status = getattr(e, "status_code", None)
    if isinstance(e, anthropic.APIError) and status:
        detail += f" (HTTP {status})"
    return (
        "llm-error", "Claude calls are failing",
        f"{detail}. The affected transactions stay unclassified and are retried on the next run.",
    )


def send_alert(kind: str, title: str, message: str) -> bool:
    """Send one alert per kind per process. True if it was sent."""
    with _lock:
        if kind in _sent:
            return False
        _sent.add(kind)
    # Escaped because the text is the caller's, and Telegram rejects the whole
    # message over a stray '<' in HTML mode.
    if not send_telegram(f"⚠️ <b>{html.escape(title)}</b>\n\n{html.escape(message)}"):
        return False
    log.info(f"Sent Telegram alert: {title}")
    return True


def llm_failure(where: str, e: Exception) -> None:
    """Report an Anthropic call that failed. `where` says which step first hit it."""
    kind, title, message = classify_error(e)
    send_alert(kind, title, f"{message}\n\nFirst seen in: {where}.")
