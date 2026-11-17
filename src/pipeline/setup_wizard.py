"""The setup command: asks the questions, checks each credential, writes
config/.env, and runs a first test backup.

    poetry run python src/pipeline/setup_wizard.py

The steps are plain functions, with the network calls passed in, so the tests
can run them without the real services. The questions and their answers are the
same either way.
"""
import json
import os
import secrets
from datetime import datetime
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
ANTHROPIC_VERSION = "2023-06-01"
TELEGRAM = "https://api.telegram.org/bot{token}/{method}"


class SetupError(Exception):
    """A check that failed, with the message to show."""


# ── The file ───────────────────────────────────────────────────────────────

def generate_secret() -> str:
    """A webhook secret: 32 random bytes, in hex."""
    return secrets.token_hex(32)


def merge_env(existing: str, updates: dict[str, str]) -> str:
    """config/.env as it was, with these keys set. A key that's already there is
    replaced in place; a new one is added at the end. Comments and other keys stay
    as they are."""
    lines = existing.splitlines()
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def write_env(updates: dict[str, str], path: Path = ENV_PATH) -> Path | None:
    """Write the answers into config/.env. The file it replaces is kept beside it,
    with a timestamp, so a wrong answer can be undone. Returns that copy, if any."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    kept = None
    if existing:
        kept = path.with_name(f"{path.name}.{datetime.now():%Y%m%d-%H%M%S}.bak")
        kept.write_text(existing, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(merge_env(existing, updates), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return kept


# ── The checks ─────────────────────────────────────────────────────────────

def check_telegram(token: str, get) -> str:
    """The bot's username, if Telegram knows the token. `get` takes a URL and returns
    the decoded JSON body."""
    body = get(TELEGRAM.format(token=token, method="getMe"))
    if not body.get("ok"):
        raise SetupError("Telegram doesn't accept that token. Copy it again from @BotFather.")
    return body["result"].get("username", "")


def find_chat_id(token: str, get) -> int | None:
    """The chat ID of the most recent message sent to the bot, or None if there isn't one
    yet. The owner sends the bot any message first, then runs this."""
    body = get(TELEGRAM.format(token=token, method="getUpdates"))
    updates = body.get("result", []) if body.get("ok") else []
    for update in reversed(updates):
        chat = (update.get("message") or {}).get("chat")
        if chat and "id" in chat:
            return int(chat["id"])
    return None


def set_telegram_webhook(token: str, url: str, secret_token: str, post) -> None:
    """Point Telegram's webhook at this server, with the secret it must send back."""
    body = post(TELEGRAM.format(token=token, method="setWebhook"),
                {"url": url, "secret_token": secret_token, "allowed_updates": ["message", "callback_query"]})
    if not body.get("ok"):
        raise SetupError(f"Telegram wouldn't set the webhook: {body.get('description', 'unknown error')}")


def check_anthropic(key: str, get) -> None:
    """A key Anthropic accepts: the model list answers only for a valid key."""
    body = get("https://api.anthropic.com/v1/models", {"x-api-key": key,
                                                       "anthropic-version": ANTHROPIC_VERSION})
    if body.get("type") == "error" or "error" in body:
        raise SetupError("Anthropic doesn't accept that key. Create one at console.anthropic.com.")


def check_database(host: str, port: str, name: str, user: str, password: str, connect) -> None:
    """A login that can reach the database and write to it. `connect` is psycopg.connect,
    passed in so tests can stand in for it."""
    try:
        con = connect(host=host, port=port, dbname=name, user=user, password=password, connect_timeout=10)
    except Exception as e:
        raise SetupError(f"Can't reach the database at {host}:{port}: {e}") from e
    try:
        con.execute("SELECT 1").fetchone()
    finally:
        con.close()


def check_folder(path: str) -> Path:
    """A folder the backups can be written to: created if missing, and checked for
    write access with a test file that's removed again."""
    folder = Path(path).expanduser()
    if not folder.is_absolute():
        raise SetupError("Give the folder's full path, e.g. /home/you/backups or C:\\Users\\you\\backups.")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".bank-enrichment-setup-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise SetupError(f"Can't write to {folder}: {e.strerror or e}") from e
    return folder


# ── The first test backup ──────────────────────────────────────────────────

def first_backup(schema_path: Path, backup_folder: Path, connect, env: dict[str, str]) -> Path:
    """Apply the schema, then take a backup into the chosen folder. This checks the whole
    path the nightly job uses. The app's own database module does the backup, so it's
    imported here, with the new settings in place."""
    os.environ.update(env)
    con = connect(**env_connect(env), autocommit=True)
    try:
        con.execute(schema_path.read_text(encoding="utf-8"))
    finally:
        con.close()
    import database_functions as db
    db.init_db()
    os.environ["BACKUP_DIR"] = str(backup_folder)
    return db.backup_db("setup-test")


def env_connect(env: dict[str, str]) -> dict:
    return {"host": env["DB_HOST"], "port": env["DB_PORT"], "dbname": env["DB_NAME"],
            "user": env["DB_USER"], "password": env["DB_PASSWORD"], "options": "-c TimeZone=UTC"}


# ── Putting it together ────────────────────────────────────────────────────

def run(answers: dict, get, post, connect, schema_path: Path, say=print) -> dict[str, str]:
    """Check every answer, write config/.env, and take the first test backup. `answers` holds
    the questions' answers. Returns the values written. Stops at the first failed check, so
    nothing is written for a wrong answer."""
    say("Checking the Telegram bot…")
    username = check_telegram(answers["TELEGRAM_BOT_TOKEN"], get)
    say(f"  bot @{username} accepts the token.")

    say("Checking the Anthropic key…")
    check_anthropic(answers["ANTHROPIC_API_KEY"], get)
    say("  the key is accepted.")

    say("Checking the database…")
    check_database(answers["DB_HOST"], answers["DB_PORT"], answers["DB_NAME"],
                   answers["DB_USER"], answers["DB_PASSWORD"], connect)
    say("  the database answers.")

    folder = check_folder(answers["BACKUP_DIR"])
    say(f"  backups can be written to {folder}.")

    values = {k: v for k, v in answers.items() if k != "PUBLIC_URL"}   # the address is for the webhook, not .env
    values["TELEGRAM_WEBHOOK_SECRET"] = generate_secret()
    values["MONZO_WEBHOOK_SECRET"] = generate_secret()

    if answers.get("PUBLIC_URL"):
        say("Registering the Telegram webhook…")
        url = answers["PUBLIC_URL"].rstrip("/") + "/receive_telegram/"
        set_telegram_webhook(answers["TELEGRAM_BOT_TOKEN"], url, values["TELEGRAM_WEBHOOK_SECRET"], post)
        say("  Telegram will send updates to it.")

    say("Writing config/.env…")
    kept = write_env(values, ENV_PATH)
    if kept:
        say(f"  the old file is kept at {kept.name}.")

    say("Taking a first test backup…")
    env = {k: values[k] for k in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")}
    path = first_backup(schema_path, folder, connect, env)
    say(f"  saved to {path}.")
    return values


# ── The command ────────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict | None = None) -> dict:
    import requests
    try:
        response = requests.get(url, headers=headers or {}, timeout=15)
        return response.json()
    except (requests.RequestException, ValueError) as e:
        raise SetupError(f"Couldn't reach {url.split('/')[2]}: {type(e).__name__}") from e


def _http_post(url: str, data: dict) -> dict:
    import requests
    try:
        response = requests.post(url, json=data, timeout=15)
        return response.json()
    except (requests.RequestException, ValueError) as e:
        raise SetupError(f"Couldn't reach {url.split('/')[2]}: {type(e).__name__}") from e


def _ask(question: str, default: str | None = None, secret: bool = False) -> str:
    import getpass
    shown = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    answer = reader(f"{question}{shown}: ").strip()
    if not answer and default is not None:
        return default
    if not answer:
        print("  This one is needed.")
        return _ask(question, default, secret)
    return answer


def _ask_chat_id(token: str) -> str:
    input("Send any message to your bot in Telegram, then press Enter here. ")
    for _ in range(3):
        chat = find_chat_id(token, _http_get)
        if chat is not None:
            print(f"  found your chat: {chat}")
            return str(chat)
        input("  No message yet. Send one to the bot, then press Enter. ")
    return _ask("Couldn't find it. Your Telegram chat ID")


def main() -> int:
    import psycopg
    print("Bank Enrichment setup. Answers are checked as you go, and config/.env is only written once they all pass.\n")
    token = _ask("Telegram bot token (from @BotFather)", secret=True)
    try:
        username = check_telegram(token, _http_get)
    except SetupError as e:
        print(e)
        return 1
    answers = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": _ask_chat_id(token)}
    print(f"  bot @{username} is yours to talk to.\n")
    answers["ANTHROPIC_API_KEY"] = _ask("Anthropic API key (console.anthropic.com)", secret=True)
    print("\nThe database: Postgres you already run, reachable from this machine.")
    answers["DB_HOST"] = _ask("Database host")
    answers["DB_PORT"] = _ask("Database port", default="5432")
    answers["DB_NAME"] = _ask("Database name")
    answers["DB_USER"] = _ask("Database user")
    answers["DB_PASSWORD"] = _ask("Database password", secret=True)
    answers["BACKUP_DIR"] = _ask("Folder for backups (a full path)",
                                 default=str(Path.home() / "bank-enrichment-backups"))
    print("\nMonzo: the login that guards linking your account (see SETUP.md, the OAuth step).")
    answers["MONZO_LOGIN_USER"] = _ask("Login name", default="admin")
    answers["MONZO_LOGIN_PASSWORD"] = _ask("Login password", secret=True)
    answers["PUBLIC_URL"] = _ask("This server's public address, e.g. https://your-name.duckdns.org "
                                 "(blank to set it later)", default="")
    print()
    try:
        run(answers, _http_get, _http_post, psycopg.connect,
            ENV_PATH.parent.parent / "sql" / "postgres" / "schema.sql")
    except SetupError as e:
        print(f"\nStopped: {e}\nNothing was written. Fix that answer and run the command again.")
        return 1
    print("\nSetup is done. Start the server with: docker compose up -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
