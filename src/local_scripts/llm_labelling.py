import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv
import anthropic

from database_functions import (
    init_db, get_unclassified, update_classification,
    get_parents, get_subcategories,
    upsert_parent, upsert_subcategory,
)

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

DB_PATH = os.getenv("DB_PATH")
CLAUDE_SECRET = os.getenv("CLAUDE_SECRET")
MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 15

LOG_PATH = os.path.join(os.path.dirname(DB_PATH), "llm_classifier.log") if DB_PATH else "llm_classifier.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)


# ── Prompt builders ────────────────────────────────────────────────────────────

def _format_transaction(t: dict) -> str:
    parts = [
        f"ID: {t['id']}",
        f"Amount: £{t['amount']:.2f}",
    ]
    if t.get("merchant_name"):
        parts.append(f"Merchant: {t['merchant_name']}")
    if t.get("description"):
        parts.append(f"Description: {t['description']}")
    if t.get("user_context"):
        parts.append(f"Context: {t['user_context']}")
    if t.get("monzo_category"):
        parts.append(f"Bank category: {t['monzo_category']}")
    return " | ".join(parts)


def _pass1_prompt(transactions: list[dict], parents: list[dict]) -> str:
    existing = ""
    if parents:
        existing = "Existing parent categories (reuse these where they fit):\n"
        for p in parents:
            existing += f"  - {p['name']} ({p['transaction_count']} transactions)\n"
    else:
        existing = "No parent categories exist yet — you will create them all.\n"

    txn_lines = "\n".join(
        f"{i+1}. {_format_transaction(t)}" for i, t in enumerate(transactions)
    )

    return f"""You are classifying personal bank transactions into parent categories for a budgeting system.

{existing}
Instructions:
- Assign each transaction to the most appropriate parent category.
- Reuse existing categories wherever they fit — only create a new one if truly needed.
- Categories should be broad (e.g. "Food & Groceries", "Transport", "Eating Out", "Health", "Shopping", "Bills & Utilities", "Holidays & Travel", "Entertainment", "Income", "Transfers").
- Use the human context field heavily — it tells you exactly what the transaction was.
- IMPORTANT: If the user context mentions "holiday" or the transaction clearly occurred during a holiday trip, ALWAYS classify it as "Holidays & Travel" — even if the spend was food, groceries, transport, or shopping. All holiday spending belongs together under one parent.
- Respond ONLY with valid JSON: an array of objects with "id" and "category" keys.
- Example: [{{"id": "tx_abc", "category": "Eating Out"}}, ...]

Transactions:
{txn_lines}"""


def _pass2_prompt(transactions: list[dict], parent_name: str, subcategories: list[dict], all_parent_names: list[str]) -> str:
    existing_subs = [s for s in subcategories if s["parent_name"] == parent_name]

    existing = ""
    if existing_subs:
        existing = f"Existing subcategories under '{parent_name}':\n"
        for s in existing_subs:
            existing += f"  - {s['name']} ({s['transaction_count']} transactions)\n"
    else:
        existing = f"No subcategories under '{parent_name}' yet — you will create them.\n"

    forbidden = ", ".join(f'"{n}"' for n in all_parent_names)

    txn_lines = "\n".join(
        f"{i+1}. {_format_transaction(t)}" for i, t in enumerate(transactions)
    )

    return f"""You are assigning subcategories to bank transactions already classified under the parent category "{parent_name}".

{existing}
Instructions:
- Assign each transaction to the most appropriate subcategory within "{parent_name}".
- Reuse existing subcategories wherever they fit.
- Keep subcategories specific but not overly granular (e.g. under "Holidays & Travel": "Accommodation", "Car Rental", "Holiday Food", "Holiday Drinks", "Holiday Shopping", "Local Transport").
- Subcategory names must NOT be the same as any parent category name. Forbidden names: {forbidden}.
- Respond ONLY with valid JSON: an array of objects with "id" and "subcategory" keys.
- Example: [{{"id": "tx_abc", "subcategory": "Accommodation"}}, ...]

Transactions:
{txn_lines}"""


# ── LLM calls ─────────────────────────────────────────────────────────────────

def _extract_json(raw: str) -> list:
    """Extract a JSON array from a response that may contain surrounding text."""
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not extract JSON array from response: {e}\nRaw: {raw[:200]}")


def classify_parents(client: anthropic.Anthropic, transactions: list[dict], parents: list[dict]) -> dict[str, str]:
    """Returns {transaction_id: parent_category_name}"""
    prompt = _pass1_prompt(transactions, parents)
    print("\n" + "="*60)
    print("PASS 1 PROMPT:")
    print("="*60)
    print(prompt)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        print("\n" + "="*60)
        print("PASS 1 RESPONSE:")
        print("="*60)
        print(raw)
        results = _extract_json(raw)
        return {r["id"]: r["category"] for r in results}
    except Exception as e:
        log.error(f"Pass 1 LLM error: {e}")
        return {}


def classify_subcategories(client: anthropic.Anthropic, transactions: list[dict], parent_name: str, subcategories: list[dict], all_parent_names: list[str]) -> dict[str, str]:
    """Returns {transaction_id: subcategory_name}"""
    prompt = _pass2_prompt(transactions, parent_name, subcategories, all_parent_names)
    print("\n" + "="*60)
    print(f"PASS 2 PROMPT ({parent_name}):")
    print("="*60)
    print(prompt)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        print("\n" + "="*60)
        print(f"PASS 2 RESPONSE ({parent_name}):")
        print("="*60)
        print(raw)
        results = _extract_json(raw)
        return {r["id"]: r["subcategory"] for r in results}
    except Exception as e:
        log.error(f"Pass 2 LLM error for '{parent_name}': {e}")
        return {}


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_start = time.time()
    log.info("--- LLM Classifier run started ---")

    if not CLAUDE_SECRET:
        log.error("CLAUDE_SECRET not set in config/.env -- exiting")
        exit(1)

    t0 = time.time()
    log.info(f"Connecting to local DB at {DB_PATH}")
    init_db()
    log.info(f"Local DB ready ({time.time() - t0:.2f}s)")

    t0 = time.time()
    log.info("Fetching unclassified transactions")
    unclassified = get_unclassified()
    log.info(f"Found {len(unclassified)} unclassified transactions ({time.time() - t0:.2f}s)")

    if not unclassified:
        log.info("Nothing to classify -- database is fully classified")
        exit()

    client = anthropic.Anthropic(api_key=CLAUDE_SECRET)

    total_classified = 0
    batches = [unclassified[i:i + BATCH_SIZE] for i in range(0, len(unclassified), BATCH_SIZE)]
    log.info(f"Processing {len(unclassified)} transactions in {len(batches)} batch(es) of up to {BATCH_SIZE}")

    for batch_num, batch in enumerate(batches, 1):
        log.info(f"--- Batch {batch_num}/{len(batches)} ({len(batch)} transactions) ---")

        # Refresh taxonomy before each batch so new categories from prior batches are visible
        parents = get_parents()
        subcategories = get_subcategories()

        # ── Pass 1: parent categories ──────────────────────────────────────────
        t0 = time.time()
        parent_map = classify_parents(client, batch, parents)
        log.info(f"Pass 1 complete ({time.time() - t0:.2f}s) — {len(parent_map)}/{len(batch)} assigned")

        if not parent_map:
            log.warning("Pass 1 returned no results — skipping batch")
            continue

        # Upsert any new parent categories
        parent_id_map: dict[str, int] = {}
        for name in set(parent_map.values()):
            parent_id_map[name] = upsert_parent(name)

        # ── Pass 2: subcategories ──────────────────────────────────────────────
        subcategories = get_subcategories()
        all_parent_names = list(parent_id_map.keys())

        by_parent: dict[str, list[dict]] = {}
        for t in batch:
            p_name = parent_map.get(t["id"])
            if p_name:
                by_parent.setdefault(p_name, []).append(t)

        sub_map: dict[str, str] = {}
        for p_name, txns in by_parent.items():
            t0 = time.time()
            result = classify_subcategories(client, txns, p_name, subcategories, all_parent_names)
            sub_map.update(result)
            log.info(f"Pass 2 '{p_name}' ({time.time() - t0:.2f}s) — {len(result)}/{len(txns)} assigned")

        # Upsert subcategories
        for txn_id, sub_name in sub_map.items():
            p_name = parent_map.get(txn_id)
            if p_name:
                upsert_subcategory(sub_name, parent_id_map[p_name])

        # ── Write classifications back ─────────────────────────────────────────
        for t in batch:
            txn_id = t["id"]
            p_name = parent_map.get(txn_id)
            s_name = sub_map.get(txn_id)
            if p_name:
                update_classification(
                    transaction_id=txn_id,
                    category=p_name,
                    subcategory=s_name,
                    confidence=None,
                    model=MODEL,
                )
                total_classified += 1
                log.info(f"  Saved {txn_id} -> {p_name} / {s_name or '—'}")
            else:
                log.warning(f"  No classification for {txn_id} — skipping")

    log.info(f"--- Run complete: {total_classified}/{len(unclassified)} classified in {time.time() - run_start:.2f}s ---")
