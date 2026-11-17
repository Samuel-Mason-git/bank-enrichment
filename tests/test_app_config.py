"""The configuration names: each setting is read under its one name."""
import app_config


def _clear(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "MONZO_LOGIN_USER", "MONZO_LOGIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_each_setting_is_read_under_its_name(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("MONZO_LOGIN_USER", "admin")
    monkeypatch.setenv("MONZO_LOGIN_PASSWORD", "pw")
    assert app_config.anthropic_key() == "key"
    assert app_config.telegram_token() == "token"
    assert app_config.monzo_login_user() == "admin"
    assert app_config.monzo_login_password() == "pw"


def test_unset_or_blank_gives_none(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert app_config.anthropic_key() is None
    assert app_config.telegram_token() is None
    assert app_config.monzo_login_user() is None
