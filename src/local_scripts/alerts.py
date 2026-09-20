"""Tell the user, on Telegram, when the pipeline can't do its job.

The classifier catches every Anthropic error and carries on -- a failed pass
returns nothing and its transactions simply stay unclassified -- which keeps one
bad call from killing a run, but it also means running out of credit produces
nothing except error lines in a log file. That is easy to miss, and the
symptom (transactions quietly not being classified) looks like nothing at all.

Only the server can reach Telegram, so this posts to its /send-alert endpoint.
Alerts carry a kind, a fix, and counts -- never transaction text. The exception
classes, not their messages, are what get reported: a JSON parse error's
message quotes the model's output, which can contain transaction details.

Nothing here may ever raise or slow the pipeline down: an alert that fails is
logged and dropped, and each kind is sent at most once per run, so a run that
fails 40 calls in a row sends one message, not 40.
"""
import logging
import os
import threading
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

LOCAL_API_KEY = os.getenv("LOCAL_API_KEY")
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")

log = logging.getLogger(__name__)

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
    if not SERVER_URL:
        return False
    try:
        response = requests.post(
            f"{SERVER_URL}/send-alert",
            headers={"X-API-Key": LOCAL_API_KEY}, json={"title": title, "message": message}, timeout=15,
        )
    except Exception as e:
        log.warning(f"Could not send Telegram alert '{title}': {e}")
        return False
    if response.status_code == 404:
        log.info("Server has no /send-alert endpoint yet — alert not sent until it is deployed")
        return False
    if not response.ok:
        log.warning(f"Telegram alert '{title}' was rejected by the server: HTTP {response.status_code}")
        return False
    log.info(f"Sent Telegram alert: {title}")
    return True


def llm_failure(where: str, e: Exception) -> None:
    """Report an Anthropic call that failed. `where` says which step first hit it."""
    kind, title, message = classify_error(e)
    send_alert(kind, title, f"{message}\n\nFirst seen in: {where}.")
