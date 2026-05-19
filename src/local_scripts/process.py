import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv
import requests
import duckdb

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

LOCAL_API_KEY = os.getenv("LOCAL_API_KEY")
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")
DB_PATH = os.getenv("DB_PATH")

LOG_PATH = os.path.splitext(DB_PATH)[0] + ".log" if DB_PATH else "process.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

_con: duckdb.DuckDBPyConnection | None = None

def get_con() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _con

def init_db() -> None:
    global _con
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _con = duckdb.connect(DB_PATH)
    sql_path = os.path.join(os.path.dirname(__file__), "..", "..", "sql", "tables.sql")
    with open(sql_path, "r") as f:
        sql = f.read()
    for statement in sql.split(";"):
        statement = statement.strip()
        if statement:
            _con.execute(statement)


HEADERS = {"X-API-Key": LOCAL_API_KEY}

def fetch_enriched():
    response = requests.get(f"{SERVER_URL}/export", headers=HEADERS)
    response.raise_for_status()
    return response.json()

def mark_processed(ids: list[str]):
    response = requests.post(
        f"{SERVER_URL}/mark-processed",
        headers=HEADERS,
        json={"ids": ids},
    )
    response.raise_for_status()
    return response.json()

def write_to_db(transactions: list[dict]):
    con = get_con()
    for t in transactions:
        data = t["payload"].get("data", {})
        merchant = data.get("merchant") or {}
        counterparty = data.get("counterparty") or {}
        con.execute(
            """INSERT INTO transactions (
                id, amount, currency, description, monzo_category,
                merchant_name, merchant_category, counterparty_name,
                is_load, created_at, settled_at, raw_payload,
                user_context, skipped, received_at, enriched_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING""",
            [
                t["id"],
                data.get("amount", 0) / 100,
                data.get("currency", "GBP"),
                data.get("description"),
                data.get("category"),
                merchant.get("name"),
                merchant.get("category"),
                counterparty.get("name"),
                data.get("is_load"),
                data.get("created"),
                data.get("settled"),
                json.dumps(t["payload"]),
                t["user_context"],
                t["skipped"],
                t["received_at"],
                t["enriched_at"],
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ]
        )
    log.info(f"Wrote {len(transactions)} transactions to local DB")



if __name__ == "__main__":
    init_db()
    enriched = fetch_enriched()
    if not enriched:
        log.info("Nothing to process")
    else:
        try:
            write_to_db(enriched)
            #mark_processed([r["id"] for r in enriched])
        except Exception as e:
            log.error(f"Processing failed: {e}", exc_info=True)

        