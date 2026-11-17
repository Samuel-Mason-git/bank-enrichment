"""The setup command: its checks, the config file it writes, and the run that ties them together.
The network and the database are stood in for, so these run anywhere."""
import os
import stat
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


@pytest.mark.skipif(os.name != "posix", reason="file modes are a POSIX thing")
def test_the_env_file_and_its_copy_are_only_readable_by_the_owner(tmp_path):
    path = tmp_path / "config" / ".env"
    path.parent.mkdir()
    path.write_text("OLD=secret\n", encoding="utf-8")
    os.chmod(path, 0o644)                       # as it was: readable by everyone
    kept = sw.write_env({"NEW": "secret2"}, path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(kept).st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="file modes are a POSIX thing")
def test_no_temporary_file_is_left_behind(tmp_path):
    path = tmp_path / "config" / ".env"
    sw.write_env({"A": "1"}, path)
    assert sorted(p.name for p in path.parent.iterdir()) == [".env"]


def test_restricting_is_a_no_op_off_windows(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(sw.os, "name", "posix")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    f = tmp_path / ".env"
    f.write_text("x", encoding="utf-8")
    sw.restrict_to_owner(f)
    assert calls == []


def test_on_windows_the_access_list_is_set_to_the_current_user(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(sw.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "mason")
    seen = []

    class Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: seen.append(cmd) or Done())
    sw.restrict_to_owner(tmp_path / ".env")
    assert seen[0][:2] == ["icacls", str(tmp_path / ".env")]
    assert "/inheritance:r" in seen[0] and "mason:F" in seen[0]


def test_a_failed_access_change_stops_the_write(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(sw.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "mason")

    class Done:
        returncode = 1
        stderr = "access denied"

    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: Done())
    with pytest.raises(sw.SetupError, match="access denied"):
        sw.restrict_to_owner(tmp_path / ".env")



# ── the daily copy's destination ───────────────────────────────────────────

class _Bucket:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def upload_file(self, path, bucket, key):
        if self.fail:
            raise RuntimeError("denied")
        self.calls.append(("upload", bucket, key))

    def delete_object(self, Bucket, Key):
        self.calls.append(("delete", Bucket, Key))


_S3_ANSWERS = {"EXPORT_DESTINATION": "s3", "EXPORT_S3_ENDPOINT": "https://acct.r2.example",
               "EXPORT_S3_BUCKET": "copies", "EXPORT_S3_PREFIX": "bank-enrichment",
               "EXPORT_S3_KEY_ID": "k", "EXPORT_S3_SECRET": "s"}


def test_no_daily_copy_needs_no_check(tmp_path):
    sw.check_destination({"EXPORT_DESTINATION": "none"})


def test_a_bucket_is_tested_with_a_file_that_is_removed_again():
    client = _Bucket()
    sw.check_destination(dict(_S3_ANSWERS), s3_factory=lambda dest: client)
    kinds = [c[0] for c in client.calls]
    assert kinds == ["upload", "delete"]
    assert client.calls[0][2] == "bank-enrichment/.bank-enrichment-setup-test"


def test_a_bucket_that_refuses_the_test_file_is_reported():
    with pytest.raises(sw.SetupError, match="didn't take a test file"):
        sw.check_destination(dict(_S3_ANSWERS), s3_factory=lambda dest: _Bucket(fail=True))


def test_a_bucket_under_bronze_is_refused_before_anything_is_sent():
    client = _Bucket()
    answers = dict(_S3_ANSWERS, EXPORT_S3_PREFIX="bronze/bank-enrichment")
    with pytest.raises(sw.SetupError, match="bronze"):
        sw.check_destination(answers, s3_factory=lambda dest: client)
    assert client.calls == []


def test_a_folder_destination_is_checked_as_a_folder(tmp_path):
    sw.check_destination({"EXPORT_DESTINATION": "folder", "EXPORT_FOLDER": str(tmp_path / "copies")})
    assert (tmp_path / "copies").is_dir()


def test_a_full_run_writes_the_destination_settings(tmp_path, monkeypatch):
    env_path = tmp_path / "config" / ".env"
    monkeypatch.setattr(sw, "ENV_PATH", env_path)
    monkeypatch.setattr(sw, "first_backup", lambda *a, **k: tmp_path / "x")
    answers = _answers(tmp_path, **_S3_ANSWERS)
    sw.run(answers, ok_get, lambda *a: {"ok": True}, lambda **kw: _Con(), tmp_path / "schema.sql",
           say=lambda *_: None, s3_factory=lambda dest: _Bucket())
    written = env_path.read_text(encoding="utf-8")
    assert "EXPORT_DESTINATION=s3" in written and "EXPORT_S3_BUCKET=copies" in written


# ── the live database: an existing one, or one run here in Docker ───────────

def _local_answers(tmp_path):
    base = _answers(tmp_path)
    base.update({"DB_LOCATION": "local", "DB_NAME": "bank_enrichment", "DB_USER": "bank_enrichment",
                 "DB_PASSWORD": "generated-password"})
    return base


def test_the_local_database_skips_the_checks_that_need_it_running(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "config" / ".env")
    monkeypatch.setattr(sw, "first_backup", lambda *a, **k: pytest.fail("no backup before the database runs"))
    reached = []

    def connect(**kwargs):
        reached.append(kwargs)
        raise AssertionError("the database isn't running yet, so it isn't contacted")

    values = sw.run(_local_answers(tmp_path), ok_get, lambda *a: {"ok": True}, connect,
                    tmp_path / "schema.sql", say=lambda *_: None)
    assert reached == []
    assert values["DB_HOST"] == "db" and "DB_LOCATION" not in values


def test_the_local_database_gets_its_own_settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "ENV_PATH", tmp_path / "config" / ".env")
    sw.run(_local_answers(tmp_path), ok_get, lambda *a: {"ok": True}, lambda **kw: _Con(),
           tmp_path / "schema.sql", say=lambda *_: None)
    db_env = (tmp_path / "config" / "db.env").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD=generated-password" in db_env and "POSTGRES_DB=bank_enrichment" in db_env
    assert "DB_HOST=db" in (tmp_path / "config" / ".env").read_text(encoding="utf-8")


def test_the_compose_file_has_the_database_behind_a_profile_on_localhost():
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'profiles: ["local-db"]' in compose
    assert '"127.0.0.1:5432:5432"' in compose
    assert "config/db.env" in compose
