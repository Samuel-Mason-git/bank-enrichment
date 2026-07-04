import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

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
