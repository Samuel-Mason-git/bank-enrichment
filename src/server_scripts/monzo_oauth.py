"""Keeps Monzo API access alive without manually re-confirming it in the
Monzo app: a confidential OAuth client (MONZO_CLIENT_ID/MONZO_CLIENT_SECRET)
exchanges the one-time authorization code for a refresh token, then this
module silently renews it on a schedule. Both the client secret and every
token stay server-side -- nothing here is ever meant to be printed or
returned to a caller."""
import asyncio
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(__file__)
TOKENS_PATH = os.path.join(BASE_DIR, "..", "..", "data", "monzo_oauth.json")
AUTH_URL = "https://auth.monzo.com/"
TOKEN_URL = "https://api.monzo.com/oauth2/token"

# How long a started authorization stays valid -- long enough to log in and
# approve in the Monzo app, short enough that a stray link can't be replayed.
STATE_TTL = 10 * 60

# state -> (redirect_uri, started_at). In memory on purpose: a restart just
# means clicking "authorize" again, and nothing here needs to outlive that.
_pending_authorizations: dict[str, tuple[str, float]] = {}

# Access tokens expire in ~6 hours (Monzo's own documented example) -- refresh
# well before that so a slow network blip never lets the stored token lapse.
REFRESH_INTERVAL = 4 * 60 * 60

MONZO_CLIENT_ID = os.getenv("MONZO_CLIENT_ID")
MONZO_CLIENT_SECRET = os.getenv("MONZO_CLIENT_SECRET")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Only re-alert once per failure streak -- reset on the next successful
# refresh -- so a lapse that persists for days sends one notice, not one
# every 4 hours.
_alerted_for_current_failure = False


def _save_tokens(response: dict) -> None:
    record = {
        "access_token": response["access_token"],
        "refresh_token": response["refresh_token"],
        "obtained_at": time.time(),
        "expires_at": time.time() + response.get("expires_in", 0),
    }
    os.makedirs(os.path.dirname(TOKENS_PATH), exist_ok=True)
    with open(TOKENS_PATH, "w") as f:
        json.dump(record, f)
    os.chmod(TOKENS_PATH, 0o600)


def _load_tokens() -> dict | None:
    if not os.path.exists(TOKENS_PATH):
        return None
    with open(TOKENS_PATH) as f:
        return json.load(f)


def begin_authorization(redirect_uri: str) -> str:
    """The Monzo login URL to send the user to, with a fresh single-use state.

    The redirect_uri is passed in (built from the request's own host) rather
    than hard-coded, so this works on anyone's domain -- it just has to match
    the redirect URL registered on their Monzo OAuth client."""
    now = time.time()
    for stale in [s for s, (_, t) in _pending_authorizations.items() if now - t > STATE_TTL]:
        del _pending_authorizations[stale]
    state = secrets.token_urlsafe(24)
    _pending_authorizations[state] = (redirect_uri, now)
    return AUTH_URL + "?" + urlencode({
        "client_id": MONZO_CLIENT_ID, "redirect_uri": redirect_uri,
        "response_type": "code", "state": state,
    })


def complete_authorization(code: str, state: str) -> bool:
    """Exchange the code only if it comes back with a state this server
    issued in the last STATE_TTL seconds -- otherwise anyone could hit the
    callback with a code of their own choosing and overwrite the stored
    tokens. Each state works once."""
    entry = _pending_authorizations.pop(state, None)
    if not entry or time.time() - entry[1] > STATE_TTL:
        log.warning("Monzo OAuth callback with an unknown or expired state -- ignored")
        return False
    return exchange_code_for_tokens(code, entry[0])


def exchange_code_for_tokens(code: str, redirect_uri: str) -> bool:
    """One-time exchange of the authorization code for the first access/
    refresh token pair. redirect_uri must be the exact one the authorization
    was started with -- Monzo rejects the exchange otherwise."""
    response = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": MONZO_CLIENT_ID,
        "client_secret": MONZO_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "code": code,
    }, timeout=20)
    if response.status_code != 200:
        log.error(f"Monzo code exchange failed: HTTP {response.status_code}")
        return False
    _save_tokens(response.json())
    log.info("Monzo OAuth tokens obtained from authorization code")
    return True


def refresh_tokens() -> bool:
    """Monzo invalidates the previous refresh token on every renewal ("refreshing
    is a one-time operation" per their docs) -- the new one must be persisted
    immediately, or the next cycle has nothing valid left to refresh with."""
    tokens = _load_tokens()
    if not tokens:
        log.warning("No Monzo tokens on disk yet -- the one-time authorization hasn't been done")
        return False
    response = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": MONZO_CLIENT_ID,
        "client_secret": MONZO_CLIENT_SECRET,
        "refresh_token": tokens["refresh_token"],
    }, timeout=20)
    if response.status_code != 200:
        log.error(f"Monzo token refresh failed: HTTP {response.status_code}")
        return False
    _save_tokens(response.json())
    log.info("Monzo OAuth tokens refreshed")
    return True


def ping() -> bool:
    """A harmless authenticated call, made right after each refresh -- Monzo's
    docs don't say whether a silent token refresh alone keeps the account's
    data-sharing confirmation "fresh", so this maximises the chance an actual
    authenticated request counts as active use."""
    tokens = _load_tokens()
    if not tokens:
        return False
    response = requests.get(
        "https://api.monzo.com/ping/whoami",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=20,
    )
    return response.status_code == 200


def run_monzo_refresh(bot) -> None:
    global _alerted_for_current_failure
    had_tokens_before = _load_tokens() is not None
    if refresh_tokens():
        ping()
        _alerted_for_current_failure = False
    elif had_tokens_before:
        # Not alerting when there's no prior authorization yet -- that's just
        # the expected state before the one-time setup, not a failure.
        log.warning("Monzo token refresh failed")
        if bot and TELEGRAM_CHAT_ID and not _alerted_for_current_failure:
            bot.send_alert(
                int(TELEGRAM_CHAT_ID),
                "Monzo access needs re-authorization",
                "The scheduled token refresh failed, so Monzo API access may lapse "
                "(merchant names on new transactions may stop coming through). "
                "Re-authorize at /oauth/start on your server to restore it.",
            )
            _alerted_for_current_failure = True


async def monzo_refresh_loop(bot) -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        try:
            run_monzo_refresh(bot)
        except Exception as e:
            log.error(f"Monzo refresh loop error: {e}", exc_info=True)
