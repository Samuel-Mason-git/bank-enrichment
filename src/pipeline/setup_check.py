"""The Settings page's setup check: is each part of the app working? Run when asked,
not on every page load, because two of the checks call Telegram and Anthropic.

Each check gives a result (ok, a warning, or a problem) and what to do about it, so a
problem shows here rather than as messages that quietly stop arriving.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import app_config
import run_schedule

PROBLEM, WARNING, OK = "problem", "warning", "ok"
PAYMENTS_QUIET_DAYS = 4      # no Monzo payment for this long usually means the webhook isn't arriving
WEBHOOK_PATHS = ("/receive_telegram/", "/recieve_telegram/")


@dataclass
class Check:
    name: str
    result: str      # PROBLEM, WARNING or OK
    detail: str


def check_database(con) -> Check:
    try:
        con.execute("SELECT 1").fetchone()
    except Exception as e:
        return Check("Database", PROBLEM, f"Not reachable: {type(e).__name__}. Check the DB_ settings in config/.env.")
    return Check("Database", OK, "Reachable.")


def check_worker(con, now: datetime) -> Check:
    last = con.execute("SELECT MAX(started_at) FROM runs").fetchone()[0]
    missed = run_schedule.missed_run(last, now)
    if last is None:
        return Check("Worker", PROBLEM, "No run recorded yet. Start it on the server with docker compose up -d.")
    when = f"{last.astimezone(run_schedule.LONDON):%a %d %b, %H:%M}"
    if missed is not None:
        return Check("Worker", PROBLEM, f"The {missed:%H:%M} run didn't start. Last run started {when}. "
                                        "On the server: docker compose ps, then docker compose logs worker.")
    status = con.execute("SELECT status FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    if status == "problems":
        return Check("Worker", WARNING, f"Last run started {when}, and a step failed. See the Activity page.")
    return Check("Worker", OK, f"Running on schedule. Last run started {when}.")


def check_payments(con, now: datetime) -> Check:
    last = con.execute("SELECT MAX(received_at) FROM transactions_raw").fetchone()[0]
    if last is None:
        return Check("Monzo payments", WARNING, "None received yet. Register the Monzo webhook (SETUP.md).")
    days = (now - last).total_seconds() / 86400
    when = f"{last.astimezone(run_schedule.LONDON):%a %d %b, %H:%M}"
    if days > PAYMENTS_QUIET_DAYS:
        return Check("Monzo payments", WARNING, f"The last one arrived {when}. If you've paid for things since, the "
                                                "Monzo webhook isn't arriving: check it's registered.")
    return Check("Monzo payments", OK, f"The last one arrived {when}.")


def check_telegram_webhook(get) -> Check:
    """get(url) returns the decoded JSON body (the network call, passed in)."""
    token = app_config.telegram_token()
    if not token:
        return Check("Telegram webhook", PROBLEM, "TELEGRAM_BOT_TOKEN isn't set in config/.env.")
    try:
        body = get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
    except Exception as e:
        return Check("Telegram webhook", WARNING, f"Couldn't ask Telegram: {type(e).__name__}.")
    if not body.get("ok"):
        return Check("Telegram webhook", PROBLEM, "Telegram doesn't accept the bot token.")
    info = body.get("result", {})
    url = info.get("url") or ""
    if not url:
        return Check("Telegram webhook", PROBLEM, "No webhook is registered, so the bot gets no messages. "
                                                  "Run the setup command, or the setWebhook step in SETUP.md.")
    if not url.endswith(WEBHOOK_PATHS):
        return Check("Telegram webhook", WARNING, f"Registered to {url}, which isn't this app's webhook path.")
    error = info.get("last_error_message")
    if error:
        return Check("Telegram webhook", WARNING, f"Registered, but Telegram's last delivery failed: {error}")
    return Check("Telegram webhook", OK, "Registered, and delivering.")


def check_claude_key(get) -> Check:
    """get(url, headers) returns the decoded JSON body."""
    key = app_config.anthropic_key()
    if not key:
        return Check("Claude key", PROBLEM, "ANTHROPIC_API_KEY isn't set in config/.env, so nothing is classified.")
    try:
        body = get("https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"})
    except Exception as e:
        return Check("Claude key", WARNING, f"Couldn't ask Anthropic: {type(e).__name__}.")
    if body.get("type") == "error" or "error" in body:
        return Check("Claude key", PROBLEM, "Anthropic doesn't accept the key. Make a new one at console.anthropic.com.")
    return Check("Claude key", OK, "Accepted.")


def _http_get(url: str, headers: dict | None = None) -> dict:
    import requests
    return requests.get(url, headers=headers or {}, timeout=15).json()


def run_all(con, get=_http_get, now: datetime | None = None) -> list[Check]:
    """Every check, in the order the page shows them. The database comes first: without it the
    others that need it are skipped."""
    now = now or datetime.now(timezone.utc)
    results = [check_database(con)]
    if results[0].result == OK:
        results += [check_worker(con, now), check_payments(con, now)]
    results += [check_telegram_webhook(get), check_claude_key(get)]
    return results

