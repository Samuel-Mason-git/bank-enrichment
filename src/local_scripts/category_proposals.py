"""Real-time category-creation proposals.

Pass 1/2 of the classifier (llm_labelling.py) are allowed to invent a parent
or subcategory name that doesn't exist yet, but a large one-off transaction
getting silently filed under the nearest existing name is exactly how a tax
payment ended up under "Professional Services". So a genuinely new name is no
longer created on the spot: the triggering transaction(s) are locked
(transactions.pending_category_proposal_id) and a Telegram card offers up to
a few candidate placements -- a new parent, a stretch-fit into an existing
one, maybe a different existing subcategory -- and asks the user to pick one,
or none.

This is deliberately a separate table and module from taxonomy_review.py's
monthly proposals, not a reuse of it: that table models reshaping transactions
that are ALREADY classified (matched by their current category, with cluster-
size guardrails that would be wrong for a single unusual payment). Here the
transactions are unclassified, membership is tracked directly by the FK above,
and there is no minimum size -- one transaction is reason enough to ask.
"""
import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from database_functions import get_con

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

LOCAL_API_KEY = os.getenv("LOCAL_API_KEY")
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")

log = logging.getLogger(__name__)


# ── Denial memory (threaded into the classifier prompts) ───────────────────────

def _denied_options() -> list[dict]:
    """Every option that appeared on a denied card -- not just whichever one
    the user might have been looking at. Denying the card rejects the whole
    set, so re-proposing any one of them is re-asking a question already
    answered.

    Excludes regenerate requests: those are "denied" under the hood (see
    collect_decisions()) so the transactions stay locked and findable, but
    asking for different options isn't a verdict that these were wrong --
    only a genuine "None of these" permanently blocks a name."""
    rows = get_con().execute(
        "SELECT options FROM category_proposals WHERE status = 'denied' AND regenerate_requested IS NOT TRUE"
    ).fetchall()
    options = []
    for (options_json,) in rows:
        options.extend(json.loads(options_json))
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


def register_group(options: list[dict], txn_ids: list[str]) -> tuple[int, bool]:
    """Attach txn_ids to a proposal offering exactly this set of candidate
    options -- merging into a still-pending proposal from an earlier
    batch/run with the identical option set if there is one, else creating a
    new one. Returns (proposal_id, was_newly_created)."""
    con = get_con()
    key = _option_key(options)
    for pid, options_json in con.execute(
        "SELECT id, options FROM category_proposals WHERE status = 'pending'"
    ).fetchall():
        if _option_key(json.loads(options_json)) == key:
            _lock(con, pid, txn_ids)
            return pid, False

    proposal_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM category_proposals").fetchone()[0]
    con.execute(
        "INSERT INTO category_proposals (id, options, status, proposed_at) VALUES (?, ?, 'pending', ?)",
        [proposal_id, json.dumps(options), time.strftime("%Y-%m-%d %H:%M:%S")],
    )
    _lock(con, proposal_id, txn_ids)
    return proposal_id, True


def _lock(con, proposal_id: int, txn_ids: list[str]) -> None:
    placeholders = ", ".join("?" for _ in txn_ids)
    con.execute(
        f"UPDATE transactions SET pending_category_proposal_id = ? WHERE id IN ({placeholders})",
        [proposal_id, *txn_ids],
    )


def pending_regenerations() -> list[dict]:
    """Proposals whose card was answered with "Try again" -- collect_decisions()
    deliberately leaves their transactions locked (rather than unlocking them
    the way a plain denial does), so the still-intact FK is what identifies
    which transactions are waiting on a fresh set of options and links them
    back to what was already shown, for llm_labelling.regenerate_category_proposals()
    to act on. Returns {"old_id", "previous_options", "txn_ids", "examples"}
    per proposal still waiting."""
    con = get_con()
    rows = con.execute(
        """SELECT DISTINCT p.id, p.options
           FROM category_proposals p
           JOIN transactions t ON t.pending_category_proposal_id = p.id
           WHERE p.status = 'denied' AND p.regenerate_requested"""
    ).fetchall()
    result = []
    for pid, options_json in rows:
        txns = con.execute(
            "SELECT id, user_context, merchant_name, counterparty_name FROM transactions WHERE pending_category_proposal_id = ?",
            [pid],
        ).fetchall()
        examples = []
        for _, user_context, merchant_name, counterparty_name in txns:
            example = user_context or merchant_name or counterparty_name
            if example and len(examples) < 4:
                examples.append(example)
        result.append({
            "old_id": pid,
            "previous_options": json.loads(options_json),
            "txn_ids": [t[0] for t in txns],
            "examples": examples,
        })
    return result


# ── Sync ─────────────────────────────────────────────────────────────────────

def server_supports_proposals() -> bool:
    """Whether the server has been deployed with the category-proposal
    endpoints yet. Without this check, a stale-deployed server would 404 on
    every sync and either crash the run or, worse, silently strand locked
    transactions with no card ever sent."""
    try:
        response = requests.get(
            f"{SERVER_URL}/category-decisions",
            headers={"X-API-Key": LOCAL_API_KEY}, timeout=30,
        )
    except Exception as e:
        log.warning(f"Could not reach the server for category proposals: {e}")
        return False
    if response.status_code == 404:
        log.info("Server has no category-proposal endpoints yet — skipping until it is deployed")
        return False
    return response.ok


def sync_new_proposals(proposal_ids: list[int]) -> None:
    """Push newly-created proposals to the server, which owns Telegram. Only
    the option names/rationales, counts and a few example contexts cross the
    wire -- never full transaction data, the same restraint taxonomy_review's
    sync uses."""
    if not proposal_ids:
        return
    con = get_con()
    proposals = []
    for pid in proposal_ids:
        row = con.execute("SELECT options FROM category_proposals WHERE id = ?", [pid]).fetchone()
        if not row:
            continue
        options = json.loads(row[0])
        txns = con.execute(
            "SELECT user_context, merchant_name, counterparty_name FROM transactions WHERE pending_category_proposal_id = ?",
            [pid],
        ).fetchall()
        examples = []
        for user_context, merchant_name, counterparty_name in txns:
            example = user_context or merchant_name or counterparty_name
            if example and len(examples) < 4:
                examples.append(example)
        proposals.append({"id": pid, "options": options, "txn_count": len(txns), "examples": examples})
    if not proposals:
        return
    response = requests.post(
        f"{SERVER_URL}/sync-category-proposals",
        headers={"X-API-Key": LOCAL_API_KEY}, json={"proposals": proposals}, timeout=30,
    )
    response.raise_for_status()
    log.info(f"Synced {len(proposals)} category proposal(s) to the server")


# ── Collection + apply ──────────────────────────────────────────────────────

def fetch_decisions() -> list[dict]:
    response = requests.get(
        f"{SERVER_URL}/category-decisions",
        headers={"X-API-Key": LOCAL_API_KEY}, timeout=30,
    )
    response.raise_for_status()
    return response.json().get("decisions", [])


def confirm_collected(ids: list[int]) -> None:
    if not ids:
        return
    requests.post(
        f"{SERVER_URL}/category-decisions/collected",
        headers={"X-API-Key": LOCAL_API_KEY},
        json={"ids": [str(i) for i in ids]}, timeout=30,
    ).raise_for_status()


def apply_selected(proposal_id: int, option_index: int) -> int:
    """Create the chosen option's category and classify exactly the
    transactions still waiting on it. Re-checks llm_category IS NULL:
    update_classification() clears the lock the moment anything else
    classifies a waiting transaction (a manual dashboard edit, a quick-tap
    match), so this never overwrites that."""
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

    pending_ids = [r[0] for r in con.execute(
        "SELECT id FROM transactions WHERE pending_category_proposal_id = ? AND llm_category IS NULL",
        [proposal_id],
    ).fetchall()]
    if not pending_ids:
        return 0

    upsert_subcategory(subcategory_name, upsert_parent(parent_name))

    placeholders = ", ".join("?" for _ in pending_ids)
    con.execute(
        f"""UPDATE transactions
            SET llm_category = ?, llm_subcategory = ?, llm_confidence = NULL,
                llm_model = 'category-proposal', classified_at = ?, pending_category_proposal_id = NULL
            WHERE id IN ({placeholders})""",
        [parent_name, subcategory_name, time.strftime("%Y-%m-%d %H:%M:%S"), *pending_ids],
    )
    log.info(f"Applied '{parent_name} / {subcategory_name}' — {len(pending_ids)} transaction(s) classified")
    return len(pending_ids)


def deny_all(proposal_id: int) -> int:
    """Unlock the waiting transactions without creating anything. They fall
    back into get_unclassified() on the next run and are reclassified with
    every option's name now forbidden in the prompt -- with no push toward an
    existing category, since rejecting every offered option is evidence none
    of them (existing or new) is the right answer."""
    con = get_con()
    ids = [r[0] for r in con.execute(
        "SELECT id FROM transactions WHERE pending_category_proposal_id = ?", [proposal_id]
    ).fetchall()]
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    con.execute(f"UPDATE transactions SET pending_category_proposal_id = NULL WHERE id IN ({placeholders})", ids)
    return len(ids)


def collect_decisions() -> int:
    """Pull Telegram decisions and act on them. Returns how many were applied."""
    con = get_con()
    try:
        decisions = fetch_decisions()
    except Exception as e:
        log.error(f"Could not fetch category decisions: {e}")
        return 0
    if not decisions:
        return 0

    applied = 0
    handled: list[int] = []
    if any(d["status"] == "selected" for d in decisions):
        from database_functions import backup_db
        backup_db("category-proposal-selected")

    for d in decisions:
        row = con.execute("SELECT status FROM category_proposals WHERE id = ?", [d["id"]]).fetchone()
        if not row or row[0] != "pending":
            handled.append(d["id"])
            continue
        try:
            if d["status"] == "selected":
                apply_selected(d["id"], d.get("selected_option"))
                con.execute(
                    """UPDATE category_proposals
                       SET status = 'applied', selected_option = ?, decided_at = ?, applied_at = ?
                       WHERE id = ?""",
                    [d.get("selected_option"), time.strftime("%Y-%m-%d %H:%M:%S"),
                     time.strftime("%Y-%m-%d %H:%M:%S"), d["id"]],
                )
                applied += 1
            elif d.get("regenerate_requested"):
                # Deliberately does NOT call deny_all(): the transactions stay
                # locked to this now-terminal proposal so pending_regenerations()
                # can find them via that FK and hand them to Claude for a fresh
                # set of options -- unlocking here would lose the link between
                # "these transactions" and "what was already shown".
                con.execute(
                    "UPDATE category_proposals SET status = 'denied', regenerate_requested = TRUE, decided_at = ? WHERE id = ?",
                    [time.strftime("%Y-%m-%d %H:%M:%S"), d["id"]],
                )
            else:
                deny_all(d["id"])
                con.execute(
                    "UPDATE category_proposals SET status = 'denied', decided_at = ? WHERE id = ?",
                    [time.strftime("%Y-%m-%d %H:%M:%S"), d["id"]],
                )
            handled.append(d["id"])
        except Exception as e:
            log.error(f"Failed to apply category decision {d['id']}: {e}", exc_info=True)

    try:
        confirm_collected(handled)
    except Exception as e:
        log.error(f"Applied decisions but could not confirm collection: {e}")
    return applied
