"""The configuration names: the current name wins, and the older one still works."""
import app_config


def _clear(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_API",
                 "MONZO_LOGIN_USER", "DASHBOARD_USER", "MONZO_LOGIN_PASSWORD", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_the_older_names_still_work(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_SECRET", "old-key")
    monkeypatch.setenv("TELEGRAM_API", "old-token")
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    assert app_config.anthropic_key() == "old-key"
    assert app_config.telegram_token() == "old-token"
    assert app_config.monzo_login_user() == "admin"
    assert app_config.monzo_login_password() == "pw"


def test_the_current_name_wins_when_both_are_set(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "new-key")
    monkeypatch.setenv("CLAUDE_SECRET", "old-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "new-token")
    monkeypatch.setenv("TELEGRAM_API", "old-token")
    assert app_config.anthropic_key() == "new-key"
    assert app_config.telegram_token() == "new-token"


def test_unset_gives_none(monkeypatch):
    _clear(monkeypatch)
    assert app_config.anthropic_key() is None
    assert app_config.monzo_login_user() is None
