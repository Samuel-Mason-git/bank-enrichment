"""Spots recurring (merchant/counterparty, category, subcategory) patterns in
already-classified transactions that have never once varied, and proposes a
Rule for them. Entirely server-side to approve -- unlike a taxonomy/category
proposal, a Rule needs no local taxonomy to create, since Rules already live
in the server's own database.

Deliberately conservative: a pattern only qualifies at MIN_OCCURRENCES+
occurrences with the category/subcategory 100% consistent across every one
of them -- a merchant that's sometimes Groceries and sometimes Household
isn't safe to auto-rule, and a one-off coincidence isn't a pattern.
"""
import logging
import os
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv

from database_functions import get_con

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

LOCAL_API_KEY = os.getenv("LOCAL_API_KEY")
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")

log = logging.getLogger(__name__)

MIN_OCCURRENCES = 5


def _dominant_context(contexts: list[str]) -> str | None:
    """Reuse whatever context you've already typed most often for this
    merchant, rather than invent new wording -- only when it's a genuine
    majority (appears in over half the contexts seen, at least twice), so a
    one-off typo or unusually detailed note doesn't become the rule's
    permanent wording."""
    if not contexts:
        return None
    value, count = Counter(contexts).most_common(1)[0]
    if count >= 2 and count > len(contexts) / 2:
        return value
    return None


def find_candidates() -> list[dict]:
    """Every (merchant_name-or-counterparty_name, category, subcategory)
    group with at least MIN_OCCURRENCES transactions and no disagreement at
    all about the category/subcategory. description is deliberately never
    used as the grouping key -- unlike a merchant name, it is rarely
    identical twice (a store number, a reference code), so grouping on it
    would produce rules too narrow to ever fire again."""
    con = get_con()
    groups = con.execute("""
        SELECT
            CASE WHEN NULLIF(merchant_name, '') IS NOT NULL THEN 'merchant_name' ELSE 'counterparty_name' END AS match_field,
            COALESCE(NULLIF(merchant_name, ''), NULLIF(counterparty_name, '')) AS match_value,
            COUNT(*) AS txn_count,
            MAX(llm_category) AS category,
            MAX(llm_subcategory) AS subcategory
        FROM transactions
        WHERE llm_category IS NOT NULL
          AND llm_subcategory IS NOT NULL
          AND skipped = FALSE
          AND COALESCE(NULLIF(merchant_name, ''), NULLIF(counterparty_name, '')) IS NOT NULL
        GROUP BY 1, 2
        HAVING COUNT(*) >= ? AND COUNT(DISTINCT llm_category || '||' || llm_subcategory) = 1
    """, [MIN_OCCURRENCES]).fetchall()

    candidates = []
    for match_field, match_value, txn_count, category, subcategory in groups:
        contexts = [r[0] for r in con.execute(
            """SELECT user_context FROM transactions
               WHERE NULLIF(user_context, '') IS NOT NULL
               AND (merchant_name = ? OR counterparty_name = ?)""",
            [match_value, match_value],
        ).fetchall()]
        candidates.append({
            "match_field": match_field, "match_value": match_value,
            "category": category, "subcategory": subcategory,
            "txn_count": txn_count,
            "auto_context": _dominant_context(contexts) or f"{match_value} purchase",
        })
    return candidates


def sync_candidates() -> int:
    """Push every current candidate to the server, which dedupes against
    what it has already asked about (or already covers with an existing
    rule) and decides what, if anything, to send on Telegram. Returns how
    many were sent in the request -- not how many the server acted on, which
    it never reports back, same restraint as every other proposal sync."""
    candidates = find_candidates()
    if not candidates:
        return 0
    try:
        response = requests.post(
            f"{SERVER_URL}/sync-rule-suggestions",
            headers={"X-API-Key": LOCAL_API_KEY}, json={"candidates": candidates}, timeout=30,
        )
    except Exception as e:
        log.warning(f"Could not reach the server to sync rule suggestions: {e}")
        return 0
    if response.status_code == 404:
        log.info("Server has no rule-suggestion endpoint yet — skipping until it is deployed")
        return 0
    response.raise_for_status()
    log.info(f"Synced {len(candidates)} rule suggestion candidate(s) to the server")
    return len(candidates)
