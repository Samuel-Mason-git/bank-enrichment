"""The setup command: its checks, the config file it writes, and the run that ties them together.
The network and the database are stood in for, so these run anywhere."""
from pathlib import Path

import pytest

import setup_wizard as sw


def ok_get(url, headers=None):
    if url.endswith("/getMe"):
        return {"ok": True, "result": {"username": "MyMoneyBot"}}
    if url.endswith("/v1/models"):
        return {"data": []}
    raise AssertionError(url)


# ── the file ───────────────────────────────────────────────────────────────

def test_a_secret_is_long_and_different_each_time():
    a, b = sw.generate_secret(), sw.generate_secret()
    assert len(a) == 64 and a != b


def test_merge_replaces_adds_and_keeps_the_rest():
    existing = "# my notes\nDB_HOST=old\nOTHER=keep\n"
    merged = sw.merge_env(existing, {"DB_HOST": "new", "TELEGRAM_CHAT_ID": "123"})
    assert merged.splitlines() == ["# my notes", "DB_HOST=new", "OTHER=keep", "TELEGRAM_CHAT_ID=123"]


def test_write_keeps_the_old_file_beside_the_new_one(tmp_path):
    path = tmp_path / "config" / ".env"
    path.parent.mkdir()
    path.write_text("OLD=1\n", encoding="utf-8")
    kept = sw.write_env({"NEW": "2"}, path)
    assert path.read_text(encoding="utf-8") == "OLD=1\nNEW=2\n"
    assert kept is not None and kept.read_text(encoding="utf-8") == "OLD=1\n"


def test_write_makes_a_new_file_when_there_was_none(tmp_path):
    path = tmp_path / "config" / ".env"
    assert sw.write_env({"A": "1"}, path) is None
    assert path.read_text(encoding="utf-8") == "A=1\n"


# ── the checks ─────────────────────────────────────────────────────────────

def test_a_good_telegram_token_gives_the_bot_name():
    assert sw.check_telegram("123:abc", ok_get) == "MyMoneyBot"


def test_a_bad_telegram_token_is_refused():
    with pytest.raises(sw.SetupError, match="@BotFather"):
        sw.check_telegram("bad", lambda url, headers=None: {"ok": False})


def test_the_chat_id_is_the_latest_message_sender():
    updates = {"ok": True, "result": [
        {"message": {"chat": {"id": 111}}}, {"message": {"chat": {"id": 222}}}]}
    assert sw.find_chat_id("t", lambda url, headers=None: updates) == 222


def test_no_message_yet_gives_no_chat_id():
    assert sw.find_chat_id("t", lambda url, headers=None: {"ok": True, "result": []}) is None


def test_the_webhook_is_set_with_its_secret_and_path():
    calls = []

    def post(url, data):
        calls.append((url, data))
        return {"ok": True}

    sw.set_telegram_webhook("t", "https://x.duckdns.org/receive_telegram/", "s3cret", post)
    url, data = calls[0]
    assert url.endswith("/setWebhook")
    assert data["url"] == "https://x.duckdns.org/receive_telegram/" and data["secret_token"] == "s3cret"


def test_a_refused_webhook_says_why():
    with pytest.raises(sw.SetupError, match="bad url"):
        sw.set_telegram_webhook("t", "u", "s", lambda url, data: {"ok": False, "description": "bad url"})


def test_a_good_anthropic_key_passes_and_a_bad_one_is_refused():
    sw.check_anthropic("sk-ok", ok_get)
    bad = lambda url, headers=None: {"type": "error", "error": {"type": "authentication_error"}}  # noqa: E731
    with pytest.raises(sw.SetupError, match="console.anthropic.com"):
        sw.check_anthropic("sk-bad", bad)


def test_the_database_check_reports_an_unreachable_host():
    def connect(**kwargs):
        raise OSError("connection refused")
    with pytest.raises(sw.SetupError, match="Can't reach the database"):
        sw.check_database("h", "5432", "d", "u", "p", connect)


def test_the_database_check_runs_a_query_and_closes(monkeypatch):
    seen = []

    class Con:
        def execute(self, sql):
            seen.append(sql)
            return self

        def fetchone(self):
            return (1,)

        def close(self):
            seen.append("closed")

    sw.check_database("h", "5432", "d", "u", "p", lambda **kw: Con())
    assert seen == ["SELECT 1", "closed"]


def test_a_relative_backup_folder_is_refused():
    with pytest.raises(sw.SetupError, match="full path"):
        sw.check_folder("relative/backups")


def test_a_backup_folder_is_created_and_checked(tmp_path):
    folder = sw.check_folder(str(tmp_path / "new" / "backups"))
    assert folder.is_dir() and not list(folder.iterdir())


# ── the run ────────────────────────────────────────────────────────────────

def _answers(tmp_path, **extra):
    base = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "sk-ok",
            "DB_HOST": "db.example", "DB_PORT": "5432", "DB_NAME": "vault", "DB_USER": "u", "DB_PASSWORD": "p",
            "BACKUP_DIR": str(tmp_path / "backups"), "MONZO_LOGIN_USER": "admin",
            "MONZO_LOGIN_PASSWORD": "pw", "PUBLIC_URL": ""}
    base.update(extra)
    return base


class _Con:
    def execute(self, sql):
        return self

    def fetchone(self):
        return (1,)

    def close(self):
        pass


def test_a_full_run_writes_the_env_and_registers_the_webhook(tmp_path, monkeypatch):
    env_path = tmp_path / "config" / ".env"
    monkeypatch.setattr(sw, "ENV_PATH", env_path)
    monkeypatch.setattr(sw, "first_backup", lambda *a, **k: tmp_path / "backups" / "x")
    posted = []
    answers = _answers(tmp_path, PUBLIC_URL="https://me.duckdns.org")
    values = sw.run(answers, ok_get, lambda url, data: posted.append((url, data)) or {"ok": True},
                    lambda **kw: _Con(), tmp_path / "schema.sql", say=lambda *_: None)
    written = env_path.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=123:abc" in written and "DB_HOST=db.example" in written
    assert "PUBLIC_URL" not in values                      # the address is for the webhook only
    assert posted and posted[0][1]["url"] == "https://me.duckdns.org/receive_telegram/"
    assert f"TELEGRAM_WEBHOOK_SECRET={values['TELEGRAM_WEBHOOK_SECRET']}" in written


def test_a_run_with_no_public_address_skips_the_webhook(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "config" / ".env")
    monkeypatch.setattr(sw, "first_backup", lambda *a, **k: tmp_path / "x")
    posted = []
    sw.run(_answers(tmp_path), ok_get, lambda *a: posted.append(a) or {"ok": True},
           lambda **kw: _Con(), tmp_path / "schema.sql", say=lambda *_: None)
    assert posted == []


def test_a_failed_check_writes_nothing(tmp_path, monkeypatch):
    env_path = tmp_path / "config" / ".env"
    monkeypatch.setattr(sw, "ENV_PATH", env_path)
    bad_anthropic = lambda url, headers=None: ({"type": "error"} if "models" in url else {"ok": True, "result": {}})  # noqa: E731
    with pytest.raises(sw.SetupError):
        sw.run(_answers(tmp_path), bad_anthropic, lambda *a: {"ok": True}, lambda **kw: _Con(),
               tmp_path / "schema.sql", say=lambda *_: None)
    assert not env_path.exists()
