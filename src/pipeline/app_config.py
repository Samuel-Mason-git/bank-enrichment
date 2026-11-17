"""The app's configuration: config/.env, read once, and the names it accepts.

Each setting has a current name and, where the name has changed, the old one,
which still works. The Anthropic key is ANTHROPIC_API_KEY (the SDK's own
default) or CLAUDE_SECRET; the Telegram token is TELEGRAM_BOT_TOKEN or
TELEGRAM_API; the Monzo OAuth login is MONZO_LOGIN_USER and MONZO_LOGIN_PASSWORD,
or DASHBOARD_USER and DASHBOARD_PASSWORD (the login used to guard the dashboard).

Read at call time, so a test or a change to the environment takes effect at once.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
load_dotenv(ENV_FILE)


def _first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def anthropic_key() -> str | None:
    return _first("ANTHROPIC_API_KEY", "CLAUDE_SECRET")


def telegram_token() -> str | None:
    return _first("TELEGRAM_BOT_TOKEN", "TELEGRAM_API")


def monzo_login_user() -> str | None:
    return _first("MONZO_LOGIN_USER", "DASHBOARD_USER")


def monzo_login_password() -> str | None:
    return _first("MONZO_LOGIN_PASSWORD", "DASHBOARD_PASSWORD")
