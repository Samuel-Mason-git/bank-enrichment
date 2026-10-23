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
import time

import requests

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(__file__)
TOKENS_PATH = os.path.join(BASE_DIR, "..", "..", "data", "monzo_oauth.json")
REDIRECT_URI = "https://bank-enrichment.duckdns.org/oauth/callback"
TOKEN_URL = "https://api.monzo.com/oauth2/token"

# Access tokens expire in ~6 hours (Monzo's own documented example) -- refresh
# well before that so a slow network blip never lets the stored token lapse.
REFRESH_INTERVAL = 4 * 60 * 60

MONZO_CLIENT_ID = os.getenv("MONZO_CLIENT_ID")
MONZO_CLIENT_SECRET = os.getenv("MONZO_CLIENT_SECRET")


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


def exchange_code_for_tokens(code: str) -> bool:
    """One-time exchange of the authorization code the /oauth/callback route
    just received for the first access/refresh token pair."""
    response = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": MONZO_CLIENT_ID,
        "client_secret": MONZO_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
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


async def monzo_refresh_loop() -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        try:
            if refresh_tokens():
                ping()
        except Exception as e:
            log.error(f"Monzo refresh loop error: {e}", exc_info=True)
