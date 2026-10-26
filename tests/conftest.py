import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub out load_dotenv before any source module is imported so the real
# config/.env file is never read and real credentials can't leak into tests.
patch("dotenv.load_dotenv", return_value=False).start()

# Set all required env vars to safe, test-only values.
os.environ["CLAUDE_SECRET"] = "test-claude-secret"
os.environ["TELEGRAM_API"] = "test-telegram-token"
os.environ["TELEGRAM_CHAT_ID"] = "12345"

_repo = Path(__file__).parent.parent
sys.path.insert(0, str(_repo / "src" / "dashboard"))
sys.path.insert(0, str(_repo / "src" / "pipeline"))
sys.path.insert(0, str(_repo / "src" / "server"))

import pytest

_PG_SCHEMA_PATH = _repo / "sql" / "postgres" / "schema.sql"


@pytest.fixture
def pg():
    """A throwaway Postgres schema with the new tables and views in it
    (sql/postgres/schema.sql), dropped again afterwards. Each test gets its own
    schema, so tests can't see each other's rows.

    Needs TEST_POSTGRES_URL, which CI sets (a Postgres service GitHub starts
    empty for each run). Without it the test is skipped, so the suite still runs
    on a machine with no Postgres -- unless REQUIRE_POSTGRES_TESTS is set, as it
    is in CI, where a missing Postgres has to fail rather than quietly skip."""
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        if os.environ.get("REQUIRE_POSTGRES_TESTS"):
            pytest.fail("REQUIRE_POSTGRES_TESTS is set but TEST_POSTGRES_URL isn't")
        pytest.skip("No TEST_POSTGRES_URL -- the Postgres tests run in CI")
    import uuid
    import psycopg
    schema = f"test_{uuid.uuid4().hex[:12]}"
    con = psycopg.connect(url, autocommit=True)
    con.execute(f"CREATE SCHEMA {schema}")
    con.execute(f"SET search_path TO {schema}")
    con.execute(_PG_SCHEMA_PATH.read_text())
    yield con
    con.execute(f"DROP SCHEMA {schema} CASCADE")
    con.close()


@pytest.fixture(autouse=True)
def _reset_db():
    """Ensure database_functions._con is None before and after every test."""
    import database_functions
    database_functions._con = None
    yield
    if database_functions._con is not None:
        try:
            database_functions._con.close()
        except Exception:
            pass
    database_functions._con = None


@pytest.fixture
def db(pg):
    """The pipeline's database, as it really connects: a connection of its own
    to the throwaway Postgres schema (the `pg` fixture), set as the connection
    database_functions hands out. Writes through it land in the same tables
    `pg` sees."""
    import psycopg
    import database_functions
    schema = pg.execute("SELECT current_schema()").fetchone()[0]
    con = psycopg.connect(os.environ["TEST_POSTGRES_URL"], autocommit=True)
    con.execute(f"SET search_path TO {schema}")
    database_functions._con = con
    yield con
    con.close()
    database_functions._con = None


@pytest.fixture
def old_local_db():
    """The PC's database as it was before the move: an in-memory DuckDB with
    sql/tables.sql. Only the copy script, which reads that shape, needs it."""
    con = duckdb.connect(":memory:")
    for stmt in _SQL_PATH.read_text().split(";"):
        s = stmt.strip()
        if s:
            con.execute(s)
    yield con
    con.close()


@pytest.fixture(autouse=True)
def alert_post(monkeypatch):
    """Keep the pipeline's Telegram messages off the network in every test, and
    start each one with nothing already sent (alerts.py sends each kind at most
    once per process). Yields the mocked requests.post so a test can assert on
    what would have been sent."""
    import alerts
    monkeypatch.setenv("TELEGRAM_API", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    alerts._sent.clear()
    with patch.object(alerts.requests, "post", return_value=MagicMock(status_code=200, ok=True)) as post:
        yield post
    alerts._sent.clear()


@pytest.fixture(autouse=True)
def retry_waits(monkeypatch):
    """Retries never really wait in a test. Yields the list of waits asked
    for, so a test can check how long something would have waited."""
    import monzo_oauth
    import telegram
    import worker
    waits = []
    for module in (telegram, monzo_oauth, worker):
        monkeypatch.setattr(module, "_sleep", waits.append)
    yield waits


@pytest.fixture(autouse=True)
def _backups_in_tmp(tmp_path, monkeypatch):
    """Every backup a test takes goes in its own temporary folder, never the
    real one (BACKUP_DIR, or ~/bank-enrichment-backups)."""
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))


@pytest.fixture(autouse=True)
def _no_real_monzo_tokens(tmp_path):
    """Point the server at a token file that doesn't exist, so a test webhook
    arriving without a merchant can never reach Monzo's real API, even on a
    machine that holds real tokens."""
    import monzo_oauth
    with patch.object(monzo_oauth, "TOKENS_PATH", str(tmp_path / "no-tokens.json")):
        yield


@pytest.fixture(autouse=True)
def _reset_server_db():
    """Ensure server_db._con is None before and after every test."""
    import server_db
    server_db._con = None
    yield
    if server_db._con is not None:
        try:
            server_db._con.close()
        except Exception:
            pass
    server_db._con = None


@pytest.fixture
def server_con(pg):
    """The server's database: a throwaway Postgres schema with the new tables
    (the `pg` fixture), set as the connection server_db hands out."""
    import server_db
    server_db._con = pg
    yield pg
    server_db._con = None
