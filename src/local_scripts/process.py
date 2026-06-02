import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv
import requests

from database_functions import init_db, write_to_db
from llm_labelling import run as run_classifier

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


if __name__ == "__main__":
    run_start = time.time()
    log.info("--- Process run started ---")

    t0 = time.time()
    log.info(f"Connecting to local DB at {DB_PATH}")
    init_db()
    log.info(f"Local DB ready ({time.time() - t0:.2f}s)")

    t0 = time.time()
    log.info(f"Fetching enriched transactions from {SERVER_URL}")
    enriched = fetch_enriched()
    log.info(f"Fetch complete ({time.time() - t0:.2f}s)")

    if not enriched:
        log.info("Nothing to process — queue is empty")
    else:
        log.info(f"Fetched {len(enriched)} transactions")
        try:
            t0 = time.time()
            write_to_db(enriched)
            log.info(f"Stored {len(enriched)} transactions ({time.time() - t0:.2f}s)")

            t0 = time.time()
            ids = [r["id"] for r in enriched]
            mark_processed(ids)
            log.info(f"Marked as processed on server ({time.time() - t0:.2f}s)")
        except Exception as e:
            log.error(f"Processing failed: {e}", exc_info=True)

    log.info(f"--- Run complete in {time.time() - run_start:.2f}s ---")

    run_classifier()
