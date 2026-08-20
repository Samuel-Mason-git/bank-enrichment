"""Real-time category-creation proposals.

Pass 1/2 of the classifier (llm_labelling.py) are allowed to invent a parent
or subcategory name that doesn't exist yet, but a large one-off transaction
getting silently filed under the nearest existing name is exactly how a tax
payment ended up under "Professional Services". So a genuinely new name is no
longer created on the spot: the triggering transaction(s) are locked
(transactions.pending_category_proposal_id) and a Telegram card asks for an
Approve/Deny before anything is created.

This is deliberately a separate table and module from taxonomy_review.py's
monthly proposals, not a reuse of it: that table models reshaping transactions
that are ALREADY classified (matched by their current category, with cluster-
size guardrails that would be wrong for a single unusual payment). Here the
transactions are unclassified, membership is tracked directly by the FK above,
and there is no minimum size -- one transaction is reason enough to ask.
"""
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

def denied_parent_names() -> set[str]:
    """Parent names the user has already declined -- Pass 1 must not propose
    these again and should prefer an existing category instead."""
    return {
        r[0].lower() for r in get_con().execute(
            "SELECT parent_name FROM category_proposals WHERE status = 'denied' AND parent_is_new"
        ).fetchall()
    }


def denied_sub_names() -> set[str]:
    """Subcategory names the user has already declined, regardless of parent --
    global like taxonomy_review's blocklist, since re-asking under a different
    parent is still re-asking the same declined idea."""
    return {
        r[0].lower() for r in get_con().execute(
            "SELECT subcategory_name FROM category_proposals WHERE status = 'denied'"
        ).fetchall()
    }


# ── Registration (called from llm_labelling.run() per novel group per batch) ──

def register_group(parent_name: str, parent_is_new: bool, subcategory_name: str, txn_ids: list[str]) -> tuple[int, bool]:
    """Attach txn_ids to a proposal for this (parent, subcategory) pair --
    merging into a still-pending one from an earlier batch/run if there is
    one, else creating it. Returns (proposal_id, was_newly_created)."""
    con = get_con()
    row = con.execute(
        """SELECT id FROM category_proposals
           WHERE status = 'pending' AND lower(parent_name) = lower(?) AND lower(subcategory_name) = lower(?)""",
        [parent_name, subcategory_name],
    ).fetchone()
    if row:
        proposal_id, is_new = row[0], False
    else:
        proposal_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM category_proposals").fetchone()[0]
        con.execute(
            """INSERT INTO category_proposals (id, parent_name, parent_is_new, subcategory_name, status, proposed_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            [proposal_id, parent_name, parent_is_new, subcategory_name, time.strftime("%Y-%m-%d %H:%M:%S")],
        )
        is_new = True
    placeholders = ", ".join("?" for _ in txn_ids)
    con.execute(
        f"UPDATE transactions SET pending_category_proposal_id = ? WHERE id IN ({placeholders})",
        [proposal_id, *txn_ids],
    )
    return proposal_id, is_new


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
    names, counts and a few example contexts cross the wire -- never full
    transaction data, the same restraint taxonomy_review's sync uses."""
    if not proposal_ids:
        return
    con = get_con()
    proposals = []
    for pid in proposal_ids:
        row = con.execute(
            "SELECT parent_name, parent_is_new, subcategory_name FROM category_proposals WHERE id = ?",
            [pid],
        ).fetchone()
        if not row:
            continue
        parent_name, parent_is_new, subcategory_name = row
        txns = con.execute(
            "SELECT user_context, merchant_name, counterparty_name FROM transactions WHERE pending_category_proposal_id = ?",
            [pid],
        ).fetchall()
        examples = []
        for user_context, merchant_name, counterparty_name in txns:
            example = user_context or merchant_name or counterparty_name
            if example and len(examples) < 4:
                examples.append(example)
        proposals.append({
            "id": pid, "parent_name": parent_name, "parent_is_new": bool(parent_is_new),
            "subcategory_name": subcategory_name, "txn_count": len(txns), "examples": examples,
        })
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


def apply_approved(proposal_id: int) -> int:
    """Create the category and classify exactly the transactions still waiting
    on it. Re-checks llm_category IS NULL: update_classification() clears the
    lock the moment anything else classifies a waiting transaction (a manual
    dashboard edit, a quick-tap match), so this never overwrites that."""
    from database_functions import upsert_parent, upsert_subcategory

    con = get_con()
    row = con.execute(
        "SELECT parent_name, subcategory_name FROM category_proposals WHERE id = ?", [proposal_id]
    ).fetchone()
    if not row:
        return 0
    parent_name, subcategory_name = row
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


def deny(proposal_id: int) -> int:
    """Unlock the waiting transactions without creating anything. They fall
    back into get_unclassified() on the next run and are reclassified with
    this name now forbidden in the prompt."""
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
    if any(d["status"] == "approved" for d in decisions):
        from database_functions import backup_db
        backup_db("category-proposal-approved")

    for d in decisions:
        row = con.execute("SELECT status FROM category_proposals WHERE id = ?", [d["id"]]).fetchone()
        if not row or row[0] != "pending":
            handled.append(d["id"])
            continue
        try:
            if d["status"] == "approved":
                apply_approved(d["id"])
                con.execute(
                    "UPDATE category_proposals SET status = 'applied', decided_at = ?, applied_at = ? WHERE id = ?",
                    [time.strftime("%Y-%m-%d %H:%M:%S"), time.strftime("%Y-%m-%d %H:%M:%S"), d["id"]],
                )
                applied += 1
            else:
                deny(d["id"])
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
