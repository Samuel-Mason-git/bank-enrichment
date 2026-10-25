import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub out load_dotenv before any source module is imported so the real
# config/.env file is never read and real credentials can't leak into tests.
patch("dotenv.load_dotenv", return_value=False).start()

# Set all required env vars to safe, test-only values.
_tmp = tempfile.gettempdir()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_bank.db")
os.environ["SERVER_URL"] = "http://test-server"
os.environ["LOCAL_API_KEY"] = "test-api-key"
os.environ["CLAUDE_SECRET"] = "test-claude-secret"
os.environ["TELEGRAM_API"] = "test-telegram-token"
os.environ["TELEGRAM_CHAT_ID"] = "12345"

_repo = Path(__file__).parent.parent
sys.path.insert(0, str(_repo / "src" / "local_scripts"))
sys.path.insert(0, str(_repo / "src" / "server_scripts"))

import pytest
import duckdb

_SQL_PATH = _repo / "sql" / "tables.sql"
_SERVER_SQL_PATH = _repo / "sql" / "server_tables.sql"
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
def db():
    """In-memory DuckDB with the full local schema applied."""
    import database_functions
    con = duckdb.connect(":memory:")
    for stmt in _SQL_PATH.read_text().split(";"):
        s = stmt.strip()
        if s:
            con.execute(s)
    database_functions._con = con
    yield con
    con.close()
    database_functions._con = None


@pytest.fixture(autouse=True)
def alert_post():
    """Keep the pipeline's Telegram alerts off the network in every test, and
    start each one with nothing already sent (alerts.py sends each kind at most
    once per process). Yields the mocked requests.post so a test can assert on
    what would have been sent."""
    import alerts
    alerts._sent.clear()
    with patch.object(alerts.requests, "post", return_value=MagicMock(status_code=200, ok=True)) as post:
        yield post
    alerts._sent.clear()


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
def server_con():
    """In-memory DuckDB with the full server schema applied."""
    import server_db
    con = duckdb.connect(":memory:")
    for stmt in _SERVER_SQL_PATH.read_text().split(";"):
        s = stmt.strip()
        if s:
            con.execute(s)
    server_db._con = con
    yield con
    con.close()
    server_db._con = None
