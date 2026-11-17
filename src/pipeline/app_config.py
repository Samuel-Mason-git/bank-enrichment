"""The app's configuration: config/.env, read once, through these accessors.

The names are ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, MONZO_LOGIN_USER and
MONZO_LOGIN_PASSWORD (the Monzo login guards linking a Monzo account). Config
written under the older names must be renamed in config/.env.

Read at call time, so a test or a change to the environment takes effect at once.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
load_dotenv(ENV_FILE)


def anthropic_key() -> str | None:
    return os.getenv("ANTHROPIC_API_KEY") or None


def telegram_token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN") or None


def monzo_login_user() -> str | None:
    return os.getenv("MONZO_LOGIN_USER") or None


def monzo_login_password() -> str | None:
    return os.getenv("MONZO_LOGIN_PASSWORD") or None


def get(name: str, default: str | None = None) -> str | None:
    """Any setting by its name, read at call time. For the settings with no accessor of their own."""
    return os.getenv(name) or default
