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

import anthropic
import requests
from dotenv import load_dotenv

import alerts
from database_functions import get_con
from llm_labelling import MODEL, _extract_json

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

LOCAL_API_KEY = os.getenv("LOCAL_API_KEY")
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")
CLAUDE_SECRET = os.getenv("CLAUDE_SECRET")

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


def _worth_a_rule_prompt(candidates: list[dict]) -> str:
    lines = [
        "For each merchant/counterparty below, decide whether it is safe to assume EVERY "
        "future transaction from it will fall under the same category shown -- i.e. is this "
        "a single-purpose merchant (a utility, subscription, or specialty service you'd only "
        "ever buy one kind of thing from), or a general-purpose retailer/marketplace where "
        "purchases could reasonably vary (a supermarket, department store, pharmacy, online "
        "marketplace, etc.)? Only single-purpose merchants are worth a rule -- a general "
        "retailer's category depends on what was actually bought that day, which a rule "
        "can't know in advance no matter how consistent it has looked so far.",
        "",
        "Respond with a JSON array only, one entry per merchant in the same order, e.g.:",
        '[{"index": 0, "worth_a_rule": true, "reasoning": "one short sentence"}, ...]',
        "",
        "Merchants:",
    ]
    for i, c in enumerate(candidates):
        lines.append(
            f'{i}. "{c["match_value"]}" — categorized as {c["category"]} > {c["subcategory"]} '
            f'in all {c["txn_count"]} past transaction(s), no exceptions'
        )
    return "\n".join(lines)


def filter_worth_a_rule(client: anthropic.Anthropic, candidates: list[dict]) -> list[dict]:
    """A merchant repeating the same category every time in YOUR history isn't
    the same as it being safe to assume for every future transaction: Lidl has
    been Groceries every time so far, but it's a general supermarket, so the
    next trip could just as easily be household goods. EE, by contrast, only
    ever sells you a phone/broadband bill. This asks Claude to tell the two
    apart using real-world knowledge the transaction history alone can't
    supply. Fails closed -- an API error drops every candidate for this run
    rather than risk suggesting an automation that turns out to be wrong;
    they're reconsidered on the next run instead."""
    if not candidates:
        return []
    prompt = _worth_a_rule_prompt(candidates)
    log.info(f"Checking whether {len(candidates)} rule candidate(s) are safe to automate")
    try:
        response = client.messages.create(
            model=MODEL, max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        verdicts = _extract_json(response.content[0].text.strip())
    except Exception as e:
        log.error(f"Rule-worthiness check failed, treating no candidates as safe this run: {e}")
        alerts.llm_failure("Rule suggestion worthiness check", e)
        return []

    kept = []
    for v in verdicts:
        index = v.get("index")
        if index is None or not (0 <= index < len(candidates)) or not v.get("worth_a_rule"):
            continue
        kept.append({**candidates[index], "llm_reasoning": v.get("reasoning", "")})
    return kept


def sync_candidates() -> int:
    """Push every current candidate to the server, which dedupes against
    what it has already asked about (or already covers with an existing
    rule) and decides what, if anything, to send on Telegram. Returns how
    many were sent in the request -- not how many the server acted on, which
    it never reports back, same restraint as every other proposal sync."""
    candidates = find_candidates()
    if not candidates:
        return 0
    if not CLAUDE_SECRET:
        log.warning("CLAUDE_SECRET not set -- skipping rule suggestions until it is")
        return 0
    candidates = filter_worth_a_rule(anthropic.Anthropic(api_key=CLAUDE_SECRET), candidates)
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
