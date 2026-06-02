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
        f"Amount: £{t['amount']:.2f} ({'money in' if t['amount'] > 0 else 'money out'})",
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


def _pass0_prompt(transactions: list[dict], subcategories: list[dict]) -> str:
    sub_lines = "\n".join(
        f"  - {s['name']} (under: {s['parent_name']})"
        for s in subcategories
    )
    txn_lines = "\n".join(
        f"{i+1}. {_format_transaction(t)}" for i, t in enumerate(transactions)
    )
    return f"""You are classifying personal bank transactions. Your job is to check whether each transaction clearly matches an existing subcategory.

Existing subcategories (with their parent category):
{sub_lines}

Instructions:
- For each transaction, return the best matching subcategory and its parent — BUT ONLY if you are confident it is the right match.
- If the transaction does not clearly fit any existing subcategory, return null for both fields.
- Do not force a match. It is better to return null than to assign the wrong category.
- Respond ONLY with valid JSON: an array of objects with "id", "category", and "subcategory" keys.
- Use null (not a string) when there is no confident match.
- Example: [{{"id": "tx_abc", "category": "Consumables", "subcategory": "Tobacco & Nicotine"}}, {{"id": "tx_xyz", "category": null, "subcategory": null}}]

Transactions:
{txn_lines}"""


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


def match_existing(client: anthropic.Anthropic, transactions: list[dict], subcategories: list[dict]) -> dict[str, dict]:
    """Returns {transaction_id: {"category": ..., "subcategory": ...}} for confident matches only."""
    if not subcategories:
        return {}
    prompt = _pass0_prompt(transactions, subcategories)
    log.info("Pass 0: matching against existing taxonomy")
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        results = _extract_json(raw)
        matched = {}
        for r in results:
            if r.get("category") and r.get("subcategory"):
                matched[r["id"]] = {"category": r["category"], "subcategory": r["subcategory"]}
        return matched
    except Exception as e:
        log.error(f"Pass 0 LLM error: {e}")
        return {}


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

def run():
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
        return

    client = anthropic.Anthropic(api_key=CLAUDE_SECRET)

    total_classified = 0
    batches = [unclassified[i:i + BATCH_SIZE] for i in range(0, len(unclassified), BATCH_SIZE)]
    log.info(f"Processing {len(unclassified)} transactions in {len(batches)} batch(es) of up to {BATCH_SIZE}")

    for batch_num, batch in enumerate(batches, 1):
        log.info(f"--- Batch {batch_num}/{len(batches)} ({len(batch)} transactions) ---")

        # Refresh taxonomy before each batch so new categories from prior batches are visible
        parents = get_parents()
        subcategories = get_subcategories()

        # ── Pass 0: match against existing taxonomy ────────────────────────────
        t0 = time.time()
        existing_map = match_existing(client, batch, subcategories)
        unmatched = [t for t in batch if t["id"] not in existing_map]
        log.info(f"Pass 0 complete ({time.time() - t0:.2f}s) — {len(existing_map)} matched, {len(unmatched)} need classification")

        # ── Pass 1: parent categories (unmatched only) ─────────────────────────
        parent_map: dict[str, str] = {}
        if unmatched:
            t0 = time.time()
            parent_map = classify_parents(client, unmatched, parents)
            log.info(f"Pass 1 complete ({time.time() - t0:.2f}s) — {len(parent_map)}/{len(unmatched)} assigned")

        # Upsert any new parent categories
        parent_id_map: dict[str, int] = {}
        for name in set(parent_map.values()):
            parent_id_map[name] = upsert_parent(name)
        for match in existing_map.values():
            name = match["category"]
            if name not in parent_id_map:
                parent_id_map[name] = upsert_parent(name)

        # ── Pass 2: subcategories (unmatched only) ─────────────────────────────
        sub_map: dict[str, str] = {}
        if unmatched and parent_map:
            subcategories = get_subcategories()
            all_parent_names = list(parent_id_map.keys())

            by_parent: dict[str, list[dict]] = {}
            for t in unmatched:
                p_name = parent_map.get(t["id"])
                if p_name:
                    by_parent.setdefault(p_name, []).append(t)

            for p_name, txns in by_parent.items():
                t0 = time.time()
                result = classify_subcategories(client, txns, p_name, subcategories, all_parent_names)
                sub_map.update(result)
                log.info(f"Pass 2 '{p_name}' ({time.time() - t0:.2f}s) — {len(result)}/{len(txns)} assigned")

            # Upsert new subcategories
            for txn_id, sub_name in sub_map.items():
                p_name = parent_map.get(txn_id)
                if p_name:
                    upsert_subcategory(sub_name, parent_id_map[p_name])

        # Upsert subcategories from Pass 0 matches (ensure they exist in taxonomy)
        for match in existing_map.values():
            p_id = parent_id_map.get(match["category"])
            if p_id:
                upsert_subcategory(match["subcategory"], p_id)

        # ── Write classifications back ─────────────────────────────────────────
        for t in batch:
            txn_id = t["id"]
            if txn_id in existing_map:
                p_name = existing_map[txn_id]["category"]
                s_name = existing_map[txn_id]["subcategory"]
            else:
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
                source = "P0" if txn_id in existing_map else "P1+2"
                log.info(f"  [{source}] {txn_id} -> {p_name} / {s_name or '—'}")
            else:
                log.warning(f"  No classification for {txn_id} — skipping")

    log.info(f"--- Run complete: {total_classified}/{len(unclassified)} classified in {time.time() - run_start:.2f}s ---")

if __name__ == "__main__":
    run()
