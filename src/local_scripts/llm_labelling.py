import json
import logging
import os
import sys
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
# Output tokens are billed per token generated, so a ceiling well above what
# a batch needs costs nothing -- while hitting it truncates the JSON, fails
# _extract_json(), and drops all BATCH_SIZE transactions from that pass. A
# full batch of 15 needs roughly 600-700 tokens; this leaves real headroom
# for longer category names and larger batches.
MAX_TOKENS = 4096

LOG_PATH = os.path.join(os.path.dirname(DB_PATH), "llm_classifier.log") if DB_PATH else "llm_classifier.log"

log = logging.getLogger(__name__)


def _configure_standalone_logging():
    """Only used when this script is run directly — when imported (e.g. by
    process.py), the importing entrypoint owns root logger configuration so
    its own log file actually receives these log lines."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
            logging.StreamHandler(stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)),
        ]
    )


# ── Prompt builders ────────────────────────────────────────────────────────────

def _payload_facts(t: dict) -> list[str]:
    """Where the purchase happened and how it was made, read out of the raw
    Monzo payload. This is context the bank already provides, so surfacing it
    to the classifier means the user doesn't have to type it -- e.g. an
    in-person purchase in Spain is evidently holiday spend without them
    writing "on holiday" on every single transaction.

    Returns an empty list rather than raising for anything unparseable: a bad
    payload should cost the prompt some detail, never abort a whole batch."""
    raw = t.get("raw_payload")
    if not raw:
        return []
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        data = payload.get("data") or {}
    except (AttributeError, TypeError, ValueError):
        return []

    facts = []
    merchant = data.get("merchant") or {}
    if merchant:
        if merchant.get("atm"):
            facts.append("Type: ATM cash withdrawal")
        elif merchant.get("online"):
            # An online merchant's address is its registered office, not
            # anywhere the user has been -- reporting it as a location would
            # turn a subscription billed from San Francisco into a trip to
            # California. Say it was online and give no place at all.
            facts.append("Type: online purchase (merchant location is not where the user was)")
        else:
            address = merchant.get("address") or {}
            where = ", ".join(
                p for p in (address.get("city"), address.get("country")) if p
            )
            if where:
                facts.append(f"Purchased in person in: {where}")

    # Monzo's own tags for the merchant (#groceries, #takeaway, ...). They
    # describe what the shop sells, not what was bought -- #groceries covers a
    # supermarket run that was actually toiletries or cigarettes -- so they go
    # in as a hint about the merchant and never as a category assignment.
    tags = (merchant.get("suggested_tags") or "").split()
    if tags:
        facts.append(
            "Merchant's general product range, NOT what was bought "
            f"(ignore entirely when the context says what it was): {' '.join(tags)}"
        )

    local_currency = data.get("local_currency")
    local_amount = data.get("local_amount")
    if local_currency and local_currency != data.get("currency") and local_amount is not None:
        # Monzo reports amounts in minor units.
        facts.append(f"Charged in {local_currency} ({abs(local_amount) / 100:.2f} {local_currency})")
    return facts


def _format_transaction(t: dict) -> str:
    parts = [
        f"ID: {t['id']}",
        f"Amount: £{t['amount']:.2f} ({'money in' if t['amount'] > 0 else 'money out'})",
    ]
    if t.get("merchant_name"):
        parts.append(f"Merchant: {t['merchant_name']}")
    # Direct debits and bank transfers populate counterparty_name instead of
    # merchant_name, and their description is a payment reference ("89GJTS7",
    # "NO REF") that says nothing. Without this the classifier sees no payee at
    # all for those and has only the user's context to go on -- which is why
    # they currently have to type "Electricity Bill" next to OCTOPUS ENERGY.
    if t.get("counterparty_name"):
        parts.append(f"Counterparty: {t['counterparty_name']}")
    if t.get("description"):
        parts.append(f"Description: {t['description']}")
    if t.get("user_context"):
        parts.append(f"Context: {t['user_context']}")
    if t.get("monzo_category"):
        parts.append(f"Bank category: {t['monzo_category']}")
    parts.extend(_payload_facts(t))
    return " | ".join(parts)


def _pass0_prompt(transactions: list[dict], subcategories: list[dict]) -> str:
    sub_lines = "\n".join(
        f"  - {s['name']} (under: {s['parent_name']})"
        for s in subcategories
    )
    txn_lines = "\n".join(
        f"{i+1}. {_format_transaction(t)}" for i, t in enumerate(transactions)
    )
    return f"""You are classifying personal bank transactions. Your job is to check whether each transaction is an unambiguous, high-confidence match for an existing subcategory.

Existing subcategories (with their parent category):
{sub_lines}

Instructions:
- Return a match ONLY if you are highly confident — the transaction clearly and obviously belongs to that subcategory with no reasonable alternative.
- If there is any doubt, any ambiguity, or any other subcategory that could plausibly fit, return null. It is far better to leave a transaction unmatched than to assign it incorrectly.
- Think carefully about overlapping subcategories — pick the most specific and accurate fit, not just the first plausible one.
- A transaction with a context like "weekly shop" clearly matches "Supermarkets". A transaction with a vague description and no context should return null.
- Respond ONLY with valid JSON: an array of objects with "id", "category", and "subcategory" keys.
- Use null (not a string) when there is no high-confidence match.
- Output ONLY the raw JSON array. No analysis, no reasoning, no markdown. Just the JSON.
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
- Assign each transaction to the single most appropriate parent category.
- Reuse existing categories wherever they genuinely fit — only create a new one if no existing category is a good match.
- Think carefully about overlap between categories. For example, a train journey could be Transport or Holidays & Travel — use the context to decide which is most accurate, not just the most obvious surface label.
- A holiday/travel category applies when the transaction is part of a trip away. Treat an in-person purchase in a foreign country as holiday spend unless the context says otherwise (a work trip, a relocation, someone living abroad) — the "Purchased in person in" line is reliable evidence of where the user actually was. An online purchase is NOT evidence of travel no matter which country the merchant is registered in, so never infer a trip from one. A flight, hotel, or Airbnb bought at home with no context does not automatically qualify, but context saying "holiday in Spain" or "weekend trip to Amsterdam" does. Regular commuting, local travel, and day-to-day transport belong in Transport.
- Use the human context field as the primary signal — it tells you what the transaction actually was.
- Prefer precision over speed: if a transaction could reasonably fit two categories, pick the one that best reflects its true purpose based on all available information.
- You may create a new parent category if none of the existing ones are a good fit, but exhaust existing options first.
- Output ONLY the raw JSON array. No analysis, no reasoning, no markdown. Just the JSON.
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
- Assign each transaction to the single most appropriate subcategory within "{parent_name}".
- Reuse existing subcategories wherever they genuinely fit — only create a new one when no existing subcategory accurately describes this transaction.
- Think carefully about overlap between existing subcategories. If two subcategories could plausibly apply, pick the one that is the best and most specific fit given all available information.
- Keep subcategories specific but not overly granular — they should be meaningful groupings, not one-off labels.
- New subcategory names should be clear, concise, and consistent in style with existing ones.
- Subcategory names must NOT be identical to any parent category name. Forbidden names: {forbidden}.
- Output ONLY the raw JSON array. No analysis, no reasoning, no markdown. Just the JSON.
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


def _warn_if_truncated(response, pass_name: str, batch_size: int) -> None:
    """A response cut off at max_tokens produces invalid JSON, which then looks
    exactly like a bad reply from the model -- and costs the entire batch. Say
    so explicitly, because the fix (raise MAX_TOKENS or lower BATCH_SIZE) is
    completely different from the fix for a genuinely malformed response."""
    if getattr(response, "stop_reason", None) == "max_tokens":
        log.error(
            f"{pass_name} hit the {MAX_TOKENS}-token output limit and was truncated -- "
            f"all {batch_size} transactions in this batch will be dropped from this pass. "
            f"Raise MAX_TOKENS or lower BATCH_SIZE (currently {BATCH_SIZE})."
        )


def match_existing(client: anthropic.Anthropic, transactions: list[dict], subcategories: list[dict]) -> dict[str, dict]:
    """Returns {transaction_id: {"category": ..., "subcategory": ...}} for confident matches only."""
    if not subcategories:
        return {}
    prompt = _pass0_prompt(transactions, subcategories)
    log.info("Pass 0: matching against existing taxonomy")
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )
        _warn_if_truncated(response, "Pass 0", len(transactions))
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
    log.info("Pass 1: classifying parent categories")
    # Prompts and responses carry your context sentences, so they sit at debug
    # level rather than being printed unconditionally -- the scheduled task
    # captures stdout, and that is not somewhere personal spending detail
    # belongs by default.
    log.debug(f"Pass 1 prompt:\n{prompt}")
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )
        _warn_if_truncated(response, "Pass 1", len(transactions))
        raw = response.content[0].text.strip()
        log.debug(f"Pass 1 response:\n{raw}")
        results = _extract_json(raw)
        return {r["id"]: r["category"] for r in results}
    except Exception as e:
        log.error(f"Pass 1 LLM error: {e}")
        return {}


def classify_subcategories(client: anthropic.Anthropic, transactions: list[dict], parent_name: str, subcategories: list[dict], all_parent_names: list[str]) -> dict[str, str]:
    """Returns {transaction_id: subcategory_name}"""
    prompt = _pass2_prompt(transactions, parent_name, subcategories, all_parent_names)
    log.info(f"Pass 2: assigning subcategories under '{parent_name}'")
    log.debug(f"Pass 2 prompt ({parent_name}):\n{prompt}")
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )
        _warn_if_truncated(response, f"Pass 2 ({parent_name})", len(transactions))
        raw = response.content[0].text.strip()
        log.debug(f"Pass 2 response ({parent_name}):\n{raw}")
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
    _configure_standalone_logging()
    run()
