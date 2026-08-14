import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv
import requests

from database_functions import (
    init_db, write_to_db, get_top_subcategories, get_top_merchant_subcategories,
    apply_quick_tap_classifications,
)
from llm_labelling import run as run_classifier
from monthly_summary import run as run_monthly_summary
from weekly_summary import run as run_weekly_summary
from taxonomy_review import run as run_taxonomy_review, collect_decisions as collect_taxonomy_decisions

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

LOCAL_API_KEY = os.getenv("LOCAL_API_KEY")
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")
DB_PATH = os.getenv("DB_PATH")

LOG_PATH = os.path.splitext(DB_PATH)[0] + ".log" if DB_PATH else "process.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=5),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

HEADERS = {"X-API-Key": LOCAL_API_KEY}


def fetch_enriched():
    response = requests.get(f"{SERVER_URL}/export", headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def mark_processed(ids: list[str]):
    response = requests.post(
        f"{SERVER_URL}/mark-processed",
        headers=HEADERS,
        json={"ids": ids},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def sync_quick_categories():
    entries = [
        {"category": row["category"], "subcategory": row["subcategory"], "merchant_name": None, "rank": i}
        for i, row in enumerate(get_top_subcategories(limit=10))
    ]
    entries += [
        {
            "category": row["category"],
            "subcategory": row["subcategory"],
            "merchant_name": row["merchant_name"],
            "rank": row["rank"] - 1,
        }
        for row in get_top_merchant_subcategories(merchant_limit=50, per_merchant_limit=3)
    ]
    response = requests.post(
        f"{SERVER_URL}/sync-quick-categories",
        headers=HEADERS,
        json={"entries": entries},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    run_start = time.time()
    log.info("--- Process run started ---")

    t0 = time.time()
    log.info(f"Connecting to local DB at {DB_PATH}")
    try:
        init_db()
        log.info(f"Local DB ready ({time.time() - t0:.2f}s)")
    except Exception as e:
        log.error(
            f"Failed to connect to local DB at {DB_PATH} "
            f"(is the Streamlit dashboard open and holding the write lock?): {e}",
            exc_info=True,
        )
        raise SystemExit(1)

    t0 = time.time()
    log.info(f"Fetching enriched transactions from {SERVER_URL}")
    try:
        enriched = fetch_enriched()
        log.info(f"Fetch complete ({time.time() - t0:.2f}s)")
    except Exception as e:
        log.error(f"Failed to fetch enriched transactions: {e}", exc_info=True)
        enriched = None

    if enriched is None:
        pass  # failure already logged above
    elif not enriched:
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

    try:
        t0 = time.time()
        quick_classified = apply_quick_tap_classifications()
        if quick_classified:
            log.info(f"Quick-tap classified {quick_classified} transactions without the LLM ({time.time() - t0:.2f}s)")
    except Exception as e:
        log.error(f"Quick-tap classification failed: {e}", exc_info=True)

    try:
        run_classifier()
    except Exception as e:
        log.error(f"LLM classification run failed: {e}", exc_info=True)

    # Decisions first, then this month's review -- so a proposal approved on the
    # phone is applied before anything new is proposed on top of it.
    try:
        applied = collect_taxonomy_decisions()
        if applied:
            log.info(f"Applied {applied} approved taxonomy proposal(s)")
    except Exception as e:
        log.error(f"Collecting taxonomy decisions failed: {e}", exc_info=True)

    try:
        proposed = run_taxonomy_review()
        if proposed:
            log.info(f"Sent {len(proposed)} taxonomy proposal(s) for review")
    except Exception as e:
        log.error(f"Taxonomy review run failed: {e}", exc_info=True)

    try:
        run_monthly_summary()
    except Exception as e:
        log.error(f"Monthly summary run failed: {e}", exc_info=True)

    try:
        run_weekly_summary()
    except Exception as e:
        log.error(f"Weekly summary run failed: {e}", exc_info=True)

    try:
        t0 = time.time()
        result = sync_quick_categories()
        log.info(f"Synced {result.get('synced', 0)} quick categories to server ({time.time() - t0:.2f}s)")
    except Exception as e:
        log.error(f"Failed to sync quick categories: {e}", exc_info=True)
