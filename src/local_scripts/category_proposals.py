"""Real-time category-creation proposals.

Pass 1/2 of the classifier (llm_labelling.py) are allowed to invent a parent
or subcategory name that doesn't exist yet, but a large one-off transaction
getting silently filed under the nearest existing name is exactly how a tax
payment ended up under "Professional Services". So a genuinely new name is no
longer created on the spot: the triggering transaction(s) are held back
(category_proposals.txn_ids) and a Telegram card offers up to a few candidate
placements -- a new parent, a stretch-fit into an existing one, maybe a
different existing subcategory -- and asks the user to pick one, or none.

The pipeline writes the proposal, whole: its options, the transactions it
holds and the lines its card shows. The server sends the card (any pending
proposal it hasn't sent yet) and records the decision on the same row, which
the next pipeline run acts on.

This is deliberately a separate table and module from taxonomy_review.py's
monthly proposals, not a reuse of it: that table models reshaping transactions
that are ALREADY classified (matched by their current category, with cluster-
size guardrails that would be wrong for a single unusual payment). Here the
transactions are unclassified, membership is tracked directly by txn_ids, and
there is no minimum size -- one transaction is reason enough to ask.
"""
import json
import logging
import time

from database_functions import get_con, release_holds, set_json, update_classification

log = logging.getLogger(__name__)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── Denial memory (threaded into the classifier prompts) ───────────────────────

def _denied_options() -> list[dict]:
    """Every option that appeared on a denied card -- not just whichever one
    the user might have been looking at. Denying the card rejects the whole
    set, so re-proposing any one of them is re-asking a question already
    answered.

    Excludes regenerate requests: those are "denied" too, but asking for
    different options isn't a verdict that these were wrong -- only a genuine
    "None of these" permanently blocks a name."""
    rows = get_con().execute(
        "SELECT options FROM category_proposals WHERE status = 'denied' AND regenerate_requested IS NOT TRUE"
    ).fetchall()
    options = []
    for (options_json,) in rows:
        card_options = json.loads(options_json)
        # A placement-judge card (placement_judge.py) offers a suggestion next to
        # the EXISTING placement it questioned. Declining it says nothing about
        # those names -- and treating them as declined would forbid an existing
        # subcategory like "Groceries" in every later classifier prompt.
        if any(o.get("judge") for o in card_options):
            continue
        options.extend(card_options)
    return options


def denied_parent_names() -> set[str]:
    """Parent names the user has already declined creating -- Pass 1 must not
    propose these again. Deliberately NOT biased toward reusing an existing
    category instead: rejecting a set of proposed placements is evidence that
    none of the existing categories were the answer either, so the retry gets
    no nudge either way beyond "not these exact names again"."""
    return {o["parent_name"].lower() for o in _denied_options() if o.get("parent_is_new")}


def denied_sub_names() -> set[str]:
    """Subcategory names the user has already declined, regardless of parent --
    global like taxonomy_review's blocklist, since re-asking under a different
    parent is still re-asking the same declined idea."""
    return {o["subcategory_name"].lower() for o in _denied_options()}


# ── Registration (called from llm_labelling.run() per novel group per batch) ──

def _option_key(options: list[dict]) -> frozenset:
    return frozenset((o["parent_name"].strip().lower(), o["subcategory_name"].strip().lower()) for o in options)


def card_example(amount, currency, created_at, merchant_name, counterparty_name, description, user_context) -> str:
    """One line on a Telegram card saying WHICH transaction it is about, e.g.
    '-£25.00 · 17 Aug · The Hub Dental Practice — "Food & Drink - Alcohol"'.

    The context alone was not enough: "Cards", "Bike ride to work" or a quick-tap
    label like "Food & Drink - Groceries" doesn't tell you which of several
    transactions a card means."""
    parts = []
    if amount is not None:
        symbol = "£" if (currency or "GBP") == "GBP" else f"{currency} "
        parts.append(f"{'-' if amount < 0 else '+'}{symbol}{abs(float(amount)):,.2f}")
    if created_at:
        parts.append(f"{created_at.day} {created_at.strftime('%b')}")
    who = (merchant_name or counterparty_name or description or "").strip()
    if who:
        parts.append(who[:40])
    line = " · ".join(parts)
    context = (user_context or "").strip()
    if context:
        line = f'{line} — "{context[:60]}"' if line else context[:60]
    return line


def _examples(con, txn_ids: list[str]) -> list[str]:
    """Up to four card lines for these transactions, oldest first."""
    if not txn_ids:
        return []
    placeholders = ", ".join("?" for _ in txn_ids)
    rows = con.execute(
        f"""SELECT CAST(amount AS DOUBLE), currency, created_at, merchant_name, counterparty_name,
                   description, user_context
            FROM transactions WHERE id IN ({placeholders}) ORDER BY created_at""",
        txn_ids,
    ).fetchall()
    return [e for e in (card_example(*r) for r in rows) if e][:4]


def held_by(con, proposal_id: int) -> list[str]:
    row = con.execute("SELECT txn_ids FROM category_proposals WHERE id = ?", [proposal_id]).fetchone()
    return json.loads(row[0]) if row else []


def register_group(options: list[dict], txn_ids: list[str]) -> tuple[int, bool]:
    """Hold txn_ids behind a proposal offering exactly this set of candidate
    options -- merging into a still-pending proposal from an earlier
    batch/run with the identical option set if there is one, else creating a
    new one. Returns (proposal_id, was_newly_created).

    A new proposal is written whole -- options, held transactions and card
    lines together -- because the server may send its card as soon as it sees
    it. A transaction is only ever held by one proposal, so it's released from
    any other first (a "Try again" card handing its transactions to the fresh
    one, say)."""
    con = get_con()
    release_holds(txn_ids)
    key = _option_key(options)
    for pid, options_json in con.execute(
        "SELECT id, options FROM category_proposals WHERE status = 'pending'"
    ).fetchall():
        if _option_key(json.loads(options_json)) == key:
            held = held_by(con, pid)
            held += [t for t in txn_ids if t not in held]
            set_json(con, "category_proposals", pid, txn_ids=held, examples=_examples(con, held))
            return pid, False

    proposal_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM category_proposals").fetchone()[0]
    con.execute(
        """INSERT INTO category_proposals (id, options, txn_ids, examples, status, proposed_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        [proposal_id, json.dumps(options), json.dumps(list(txn_ids)), json.dumps(_examples(con, txn_ids)), _now()],
    )
    return proposal_id, True


def sync_new_proposals(proposal_ids: list[int]) -> None:
    """Nothing to send any more: the server sends the card for every proposal
    it hasn't sent yet. Kept so the callers can say what they've made."""
    if proposal_ids:
        log.info(f"{len(proposal_ids)} new category proposal(s); the server sends their cards")


def pending_regenerations() -> list[dict]:
    """Proposals whose card was answered with "Try again" -- their transactions
    are deliberately still held (rather than released the way a plain denial
    is), so the hold is what identifies which transactions are waiting on a
    fresh set of options and links them back to what was already shown, for
    llm_labelling.regenerate_category_proposals() to act on. Returns
    {"old_id", "previous_options", "txn_ids", "examples"} per proposal still waiting."""
    con = get_con()
    result = []
    for pid, options_json, txn_ids_json in con.execute(
        """SELECT id, options, txn_ids FROM category_proposals
           WHERE status = 'denied' AND regenerate_requested AND txn_ids <> '[]'"""
    ).fetchall():
        txn_ids = json.loads(txn_ids_json)
        placeholders = ", ".join("?" for _ in txn_ids)
        examples = []
        for user_context, merchant_name, counterparty_name in con.execute(
            f"SELECT user_context, merchant_name, counterparty_name FROM transactions WHERE id IN ({placeholders})",
            txn_ids,
        ).fetchall():
            example = user_context or merchant_name or counterparty_name
            if example and len(examples) < 4:
                examples.append(example)
        result.append({
            "old_id": pid,
            "previous_options": json.loads(options_json),
            "txn_ids": txn_ids,
            "examples": examples,
        })
    return result


# ── Acting on decisions ─────────────────────────────────────────────────────

def apply_selected(proposal_id: int, option_index: int) -> int:
    """Create the chosen option's category and classify exactly the
    transactions still waiting on it. Re-checks llm_category IS NULL:
    classifying a transaction by any other path (a manual dashboard edit, a
    quick-tap match) releases its hold, so this never overwrites that.

    The one exception is a transaction the placement judge is holding while it
    is still classified (judge_backlog.py reviews history, and leaves it where
    it is until you decide). Those are recognised by their 'held' row in
    judge_reviews."""
    from database_functions import upsert_parent, upsert_subcategory

    con = get_con()
    row = con.execute("SELECT options FROM category_proposals WHERE id = ?", [proposal_id]).fetchone()
    if not row:
        return 0
    options = json.loads(row[0])
    if option_index is None or not (0 <= option_index < len(options)):
        log.error(f"Category proposal {proposal_id}: option index {option_index} out of range for {len(options)} option(s)")
        return 0
    chosen = options[option_index]
    parent_name, subcategory_name = chosen["parent_name"], chosen["subcategory_name"]

    held = held_by(con, proposal_id)
    if not held:
        return 0
    placeholders = ", ".join("?" for _ in held)
    pending_ids = [r[0] for r in con.execute(
        f"""SELECT id FROM transactions
            WHERE id IN ({placeholders})
            AND (llm_category IS NULL
                 OR id IN (SELECT txn_id FROM judge_reviews WHERE outcome = 'held'))""",
        held,
    ).fetchall()]
    release_holds(held)
    if not pending_ids:
        return 0

    upsert_subcategory(subcategory_name, upsert_parent(parent_name))
    for txn_id in pending_ids:
        update_classification(txn_id, parent_name, subcategory_name, None, "category-proposal")
    # Only rows the placement judge is still holding have anything to update.
    # 'kept' is the standing answer that stops the same merchant being
    # questioned about the same placement again.
    placeholders = ", ".join("?" for _ in pending_ids)
    con.execute(
        f"""UPDATE judge_reviews SET outcome = ?, reviewed_at = ?
            WHERE outcome = 'held' AND txn_id IN ({placeholders})""",
        ["kept" if chosen.get("is_original") else "changed", _now(), *pending_ids],
    )
    log.info(f"Applied '{parent_name} / {subcategory_name}' — {len(pending_ids)} transaction(s) classified")
    return len(pending_ids)


def deny_all(proposal_id: int) -> int:
    """Release the waiting transactions without creating anything. They fall
    back into get_unclassified() on the next run and are reclassified with
    every option's name now forbidden in the prompt -- with no push toward an
    existing category, since rejecting every offered option is evidence none
    of them (existing or new) is the right answer."""
    con = get_con()
    held = held_by(con, proposal_id)
    release_holds(held)
    return len(held)


def collect_decisions() -> int:
    """Act on the decisions made on Telegram since the last run (the server
    records them on the proposal itself). Returns how many were applied.

    A choice is applied and the proposal marked 'applied'. "None of these"
    releases the transactions. "Try again" keeps them held, for
    regenerate_category_proposals() to hand to a fresh proposal."""
    con = get_con()
    applied = 0
    for proposal_id, selected_option in con.execute(
        "SELECT id, selected_option FROM category_proposals WHERE status = 'selected' ORDER BY id"
    ).fetchall():
        try:
            apply_selected(proposal_id, selected_option)
            con.execute("UPDATE category_proposals SET status = 'applied', applied_at = ? WHERE id = ?",
                        [_now(), proposal_id])
            applied += 1
        except Exception as e:
            log.error(f"Failed to apply category decision {proposal_id}: {e}", exc_info=True)

    for (proposal_id,) in con.execute(
        """SELECT id FROM category_proposals
           WHERE status = 'denied' AND regenerate_requested IS NOT TRUE AND txn_ids <> '[]'"""
    ).fetchall():
        try:
            deny_all(proposal_id)
        except Exception as e:
            log.error(f"Failed to release category proposal {proposal_id}: {e}", exc_info=True)
    return applied
