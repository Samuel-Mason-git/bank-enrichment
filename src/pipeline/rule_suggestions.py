"""Spots recurring (merchant/counterparty, category, subcategory) patterns in
already-classified transactions that have never once varied, and proposes a
Rule for them: a row in rule_suggestions, whose Telegram card the server
sends. Approving it there creates the rule.

Deliberately conservative: a pattern only qualifies at MIN_OCCURRENCES+
occurrences with the category/subcategory 100% consistent across every one
of them -- a merchant that's sometimes Groceries and sometimes Household
isn't safe to auto-rule, and a one-off coincidence isn't a pattern.
"""
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import alerts
from database_functions import get_con, insert_with_next_id
from llm_labelling import MODEL, _extract_json

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

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
        HAVING COUNT(*) >= %s AND COUNT(DISTINCT llm_category || '||' || llm_subcategory) = 1
    """, [MIN_OCCURRENCES]).fetchall()

    candidates = []
    for match_field, match_value, txn_count, category, subcategory in groups:
        contexts = [r[0] for r in con.execute(
            """SELECT user_context FROM transactions
               WHERE NULLIF(user_context, '') IS NOT NULL
               AND (merchant_name = %s OR counterparty_name = %s)""",
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


def filter_worth_a_rule(client: anthropic.Anthropic, candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """A merchant repeating the same category every time in YOUR history isn't
    the same as it being safe to assume for every future transaction: Lidl has
    been Groceries every time so far, but it's a general supermarket, so the
    next trip could just as easily be household goods. EE, by contrast, only
    ever sells you a phone/broadband bill. This asks Claude to tell the two
    apart using real-world knowledge the transaction history alone can't
    supply.

    Returns (worth a rule, declined), each with Claude's reasoning. Declined
    means an explicit "not worth a rule" -- remembered, so the same pattern
    isn't paid for again every run. Fails closed: an API error, or an answer
    with no verdict for a candidate, puts it in neither list, so it's simply
    reconsidered next run rather than suggested unchecked or dismissed for
    good on a glitch."""
    if not candidates:
        return [], []
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
        return [], []

    kept, declined = [], []
    for v in verdicts:
        index = v.get("index")
        if not isinstance(index, int) or not (0 <= index < len(candidates)) or "worth_a_rule" not in v:
            continue
        judged = {**candidates[index], "llm_reasoning": v.get("reasoning", "")}
        (kept if v["worth_a_rule"] is True else declined).append(judged)
    return kept, declined


def _text_matches(value: str, match_type: str, pattern: str) -> bool:
    """Whether a rule's condition matches a merchant/counterparty name -- the
    same tests the server's rules apply. Amount-type conditions never match a
    name."""
    try:
        if match_type == "exact":
            return value.lower() == pattern.lower()
        if match_type == "contains":
            return pattern.lower() in value.lower()
        if match_type == "regex":
            return bool(re.search(pattern, value, re.IGNORECASE))
    except re.error:
        return False
    return False


def _not_yet_asked(con, candidates: list[dict]) -> list[dict]:
    """Drop a candidate already suggested -- in any status, so a denied one
    stays denied and one Claude declined isn't asked about again -- and one an
    existing enabled rule already catches on its own.
    A rule with a second condition can't be judged without a whole transaction,
    so it doesn't count as covering one: better to still suggest than to
    silently miss a useful rule. Done before the worthiness check, so Claude is
    only ever asked about a pattern once."""
    asked = {(f, v) for f, v in con.execute("SELECT match_field, match_value FROM rule_suggestions").fetchall()}
    rules = con.execute(
        """SELECT match_field, match_type, match_value FROM rules
           WHERE enabled AND (match_field_2 IS NULL OR match_field_2 = '')"""
    ).fetchall()
    return [
        c for c in candidates
        if (c["match_field"], c["match_value"]) not in asked
        and not any(field == c["match_field"] and _text_matches(c["match_value"], match_type, value)
                    for field, match_type, value in rules)
    ]


def _store(con, c: dict, status: str) -> None:
    insert_with_next_id(
        con,
        """INSERT INTO rule_suggestions
           (id, match_field, match_value, category, subcategory, txn_count, auto_context,
            llm_reasoning, status, proposed_at)
           VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM rule_suggestions), %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        [c["match_field"], c["match_value"], c["category"], c["subcategory"],
         c["txn_count"], c["auto_context"], c.get("llm_reasoning", ""), status, time.strftime("%Y-%m-%d %H:%M:%S")],
        "rule_suggestions",
    )


def sync_candidates() -> int:
    """Suggest a rule for every new candidate Claude judges safe to automate:
    a pending row in rule_suggestions, whose card the server then sends. One
    Claude declines is stored as 'declined' -- no card, and never asked about
    again. Returns how many were suggested."""
    con = get_con()
    candidates = _not_yet_asked(con, find_candidates())
    if not candidates:
        return 0
    if not CLAUDE_SECRET:
        log.warning("CLAUDE_SECRET not set -- skipping rule suggestions until it is")
        return 0
    worth, declined = filter_worth_a_rule(anthropic.Anthropic(api_key=CLAUDE_SECRET), candidates)
    for c in worth:
        _store(con, c, "pending")
    for c in declined:
        _store(con, c, "declined")
    if worth:
        log.info(f"Suggested {len(worth)} rule(s); the server sends their cards")
    if declined:
        log.info(f"{len(declined)} pattern(s) not worth a rule -- remembered, so not asked again")
    return len(worth)
