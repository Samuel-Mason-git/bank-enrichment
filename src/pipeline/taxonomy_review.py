"""Monthly taxonomy-gap review.

The classifier sees transactions one batch at a time, so it can create a
subcategory when a *single* transaction fits nothing -- but it can never notice
that a *pattern* across many transactions deserves its own label. Eight
breakfasts filed under "Lunches Out" over three months are obvious in aggregate
and invisible per-batch.

This runs once a month over whole subcategories rather than batches, asks for
coherent clusters, and enforces hard numeric guardrails on whatever comes back.
It only ever proposes: nothing is created or moved until the user approves the
Telegram card.
"""
import json
import logging
import os
import time
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from database_functions import get_con, init_db, insert_with_next_id, now

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

CLAUDE_SECRET = os.getenv("CLAUDE_SECRET")
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048

# ── Guardrails ────────────────────────────────────────────────────────────────
# Deliberately strict. A proposal costs the user a decision and, if approved,
# permanently reshapes the taxonomy -- so the bar is "obviously its own thing",
# not "arguably separable". These are enforced in code AFTER the model answers,
# because soft prompt wording alone has already proven insufficient elsewhere
# in this pipeline (the merchant-tag override).
MIN_SUBCATEGORY_SIZE = 8      # below this there isn't enough to judge
MIN_CLUSTER_SIZE = 6          # a cluster smaller than this is a one-off
MIN_CLUSTER_SHARE = 0.25      # must be a real slice of its parent
MAX_CLUSTER_SHARE = 0.90      # taking nearly all of it is a rename, not a split
MAX_PROPOSALS_PER_RUN = 2     # at most two decisions to make per month

log = logging.getLogger(__name__)


# ── Pure logic (unit tested without a database or an API) ─────────────────────

def run_key(today: date | None = None) -> str:
    """One review per calendar month."""
    return (today or date.today()).strftime("%Y-%m")


def filter_proposals(
    proposals: list[dict],
    source_size: int,
    valid_ids: set[str],
    taxonomy: dict[str, list[str]],
    source_parent: str = "",
    source_sub: str = "",
    blocked_names: set[str] | None = None,
) -> list[dict]:
    """Apply the guardrails to whatever the model proposed.

    Two actions are allowed and each has the opposite existence requirement: a
    "create" must name something that does NOT exist, a "move" must name
    something that DOES. Both are re-checked here rather than trusted to the
    prompt, so a model that ignores the instruction cannot invent a duplicate
    category or move transactions into a subcategory that isn't there.

    Evidence ids are intersected with `valid_ids` rather than trusted: the model
    is echoing ids back from a prompt, and an id that isn't actually in this
    subcategory would move a transaction the user never reviewed."""
    existing = {s.lower(): (p, s) for p, subs in taxonomy.items() for s in subs}
    parents = {p.lower() for p in taxonomy}
    kept = []
    for p in proposals:
        action = (p.get("action") or "create").strip().lower()
        name = (p.get("target_sub") or p.get("proposed_sub") or "").strip()
        rationale = (p.get("rationale") or "").strip()
        ids = [i for i in dict.fromkeys(p.get("evidence_ids") or []) if i in valid_ids]
        if not name or not rationale or action not in ("create", "move"):
            continue

        if action == "create":
            if name.lower() in existing or name.lower() in parents:
                log.info(f"Rejected create '{name}': that category already exists")
                continue
            # Names already put to the user in an earlier month, whatever they
            # answered. Re-asking a question they already declined is nagging.
            if name.lower() in (blocked_names or set()):
                log.info(f"Rejected create '{name}': already proposed in an earlier run")
                continue
            target_parent = (p.get("target_parent") or source_parent).strip() or source_parent
        else:
            if name.lower() not in existing:
                log.info(f"Rejected move to '{name}': no such subcategory")
                continue
            target_parent, name = existing[name.lower()]
            if (target_parent, name) == (source_parent, source_sub):
                log.info(f"Rejected move to '{name}': that is where they already are")
                continue

        if len(ids) < MIN_CLUSTER_SIZE:
            log.info(f"Rejected proposal '{name}': {len(ids)} transactions < {MIN_CLUSTER_SIZE}")
            continue
        share = len(ids) / source_size if source_size else 0
        if share < MIN_CLUSTER_SHARE:
            log.info(f"Rejected proposal '{name}': {share:.0%} of parent < {MIN_CLUSTER_SHARE:.0%}")
            continue
        if share > MAX_CLUSTER_SHARE:
            log.info(f"Rejected proposal '{name}': {share:.0%} of parent is a rename, not a split")
            continue

        kept.append({**p, "action": action, "target_sub": name,
                     "target_parent": target_parent, "rationale": rationale,
                     "evidence_ids": ids, "evidence_count": len(ids)})
    # Biggest, most clear-cut clusters first, so the cap keeps the best ones.
    kept.sort(key=lambda p: p["evidence_count"], reverse=True)
    return kept[:MAX_PROPOSALS_PER_RUN]


def _identity(row: dict) -> tuple[str, str] | None:
    """What makes two transactions indistinguishable to the user: the same payee
    and the same thing written about it. None when either is missing, so sparse
    rows never match each other on emptiness alone."""
    merchant = (row.get("merchant") or "").strip().casefold()
    context = (row.get("context") or "").strip().casefold()
    return (merchant, context) if (merchant and context) else None


def expand_evidence(evidence_ids: list[str], rows: list[dict]) -> list[str]:
    """Pull in every transaction indistinguishable from one already in the
    evidence.

    The model writes out ids by hand and does not do it consistently. Seven
    identical "Train tickets for work" at Trip.com produced six inside the
    proposed group and one outside, so applying it left two identical
    transactions in different subcategories -- a difference no user can be
    shown a reason for, because there isn't one.

    Expanding here rather than at apply time is deliberate: the card has to
    state the true count, or "approving moves only the transactions listed on
    that card" stops being true."""
    by_id = {r["id"]: r for r in rows}
    keys = {k for i in evidence_ids if (k := _identity(by_id.get(i) or {})) is not None}
    expanded = dict.fromkeys(evidence_ids)
    if keys:
        for r in rows:
            if _identity(r) in keys:
                expanded[r["id"]] = None
    return list(expanded)


def build_prompt(parent: str, subcategory: str, rows: list[dict],
                 taxonomy: dict[str, list[str]] | None = None) -> str:
    lines = "\n".join(
        f'  {r["id"]} | £{abs(float(r["amount"])):.2f} | {r.get("merchant") or "—"} | {r.get("context") or "—"}'
        for r in rows
    )
    # Without the rest of the taxonomy the reviewer is blind in the same way the
    # per-batch classifier is: it cannot see that a mislabelled group already has
    # a home, so "create a new subcategory" becomes its only available answer,
    # and it can propose a name that already exists elsewhere.
    taxonomy_block = ""
    if taxonomy:
        listed = "\n".join(
            f"  {p}: {', '.join(sorted(subs))}" for p, subs in sorted(taxonomy.items())
        )
        taxonomy_block = f"""
Your existing categories and subcategories:
{listed}
"""
    return f"""Below is EVERY transaction currently filed under the subcategory "{subcategory}" (inside the parent category "{parent}").
{taxonomy_block}

Your job is to find transactions that the name "{subcategory}" actively MISDESCRIBES, and that form a group large and coherent enough to deserve a subcategory of their own.

The test is not "are these different from each other". Almost any group of transactions can be subdivided somehow, and doing so is harmful — it creates a taxonomy too fine-grained to maintain. The test is whether calling these transactions "{subcategory}" is simply WRONG.

Be extremely conservative. Returning an empty list is the correct and expected answer most of the time.

Do NOT propose a group that differs only by:
- where it was bought (a supermarket lunch and a restaurant lunch are both lunch)
- format, brand, or price point (a £3 meal deal and a £10 sit-down meal are both the same meal)
- the specific product, when the occasion is the same ("Bread" and "Milk" are both a grocery shop)
- being cheaper, quicker, or more convenient versions of the same thing

DO propose when the transactions are a genuinely different KIND of thing from what the subcategory name claims — a different occasion, or a different PURPOSE for the spending:
- a breakfast is not a lunch — different meal, so "Breakfast wrap" does not belong in "Lunches Out"
- regular travel to work is a fixed, recurring commitment, not the same kind of spending as occasional discretionary travel — different purpose
- a gift bought for someone else is not shopping for yourself

Note carefully how these differ from the exclusions above. The exclusions are all the SAME purpose bought a different way. These are the same sort of item bought for a DIFFERENT reason. Purpose is what matters, never venue, format or price.

When you do find such a group, there are two possible answers, and you must pick the right one:

1. "move" — the group belongs in a subcategory that ALREADY EXISTS in the list above. Prefer this whenever something in that list genuinely fits, since a near-duplicate of an existing category is the worst possible outcome.
2. "create" — nothing in the list above fits. This is a perfectly good answer and you should use it without hesitation when it is true. Check the list first, but do not force a bad "move" onto a group that has no real home yet.

Further rules:
- A group must contain at least {MIN_CLUSTER_SIZE} of the transactions listed.
- Do NOT propose a group that covers nearly all of these transactions — that is a rename, not a split, and is not wanted.
- A new subcategory name must be short, specific, and consistent in style with the existing ones.
- Never "create" a name that already appears in the list above, and never "move" to a subcategory that is not in it.
- Every id you list in evidence_ids must be copied exactly from the list below.

Respond ONLY with a raw JSON array, no markdown and no commentary. Each object must have:
  "action": either "move" or "create"
  "target_sub": the existing subcategory to move into, or the new one to create
  "target_parent": the parent category it sits under (for "move", the parent it already has above)
  "rationale": one short sentence the user will read on a phone
  "evidence_ids": the exact transaction ids belonging to that group
An empty array [] means nothing here is mislabelled.

Transactions ({len(rows)} total) — id | amount | merchant | context:
{lines}"""


# ── Database reads ────────────────────────────────────────────────────────────

def reviewable_subcategories() -> list[dict]:
    """Subcategories with enough transactions to judge, biggest first."""
    rows = get_con().execute(
        """SELECT llm_category, llm_subcategory, COUNT(*) AS n
           FROM transactions
           WHERE llm_category IS NOT NULL AND llm_subcategory IS NOT NULL
           GROUP BY 1, 2 HAVING COUNT(*) >= %s
           ORDER BY n DESC""",
        [MIN_SUBCATEGORY_SIZE],
    ).fetchall()
    return [{"parent": r[0], "subcategory": r[1], "size": r[2]} for r in rows]


def transactions_in(parent: str, subcategory: str) -> list[dict]:
    rows = get_con().execute(
        """SELECT id, amount, merchant_name, counterparty_name, user_context
           FROM transactions
           WHERE llm_category = %s AND llm_subcategory = %s
           ORDER BY created_at""",
        [parent, subcategory],
    ).fetchall()
    return [{"id": r[0], "amount": r[1], "merchant": r[2] or r[3], "context": r[4]} for r in rows]


def full_taxonomy() -> dict[str, list[str]]:
    """{parent: [subcategory, ...]} -- shown to the reviewer so it can prefer
    moving into a category that already exists over inventing a near-duplicate."""
    rows = get_con().execute(
        """SELECT p.name, s.name FROM subcategories s
           JOIN parent_categories p ON p.id = s.parent_id
           WHERE s.archived_at IS NULL AND p.archived_at IS NULL
           ORDER BY p.name, s.name"""
    ).fetchall()
    taxonomy: dict[str, list[str]] = {}
    for parent, sub in rows:
        taxonomy.setdefault(parent, []).append(sub)
    return taxonomy


def already_reviewed(key: str) -> bool:
    return get_con().execute(
        "SELECT COUNT(*) FROM taxonomy_proposals WHERE run_key = %s", [key]
    ).fetchone()[0] > 0


def previously_proposed_names() -> set[str]:
    """A name the user already denied must not come back next month -- that is
    nagging, and it is the fastest way to make them ignore the cards entirely."""
    return {
        r[0].lower()
        for r in get_con().execute("SELECT proposed_sub FROM taxonomy_proposals").fetchall()
    }


# ── LLM ───────────────────────────────────────────────────────────────────────

def _extract_json(raw: str) -> list:
    try:
        return json.loads(raw[raw.index("["):raw.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not extract JSON array: {e}\nRaw: {raw[:200]}")


def review_subcategory(client, parent: str, subcategory: str, rows: list[dict],
                       taxonomy: dict[str, list[str]],
                       blocked_names: set[str] | None = None) -> list[dict]:
    prompt = build_prompt(parent, subcategory, rows, taxonomy)
    log.debug(f"Taxonomy review prompt for {parent}/{subcategory}:\n{prompt}")
    try:
        response = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        log.debug(f"Taxonomy review response for {subcategory}:\n{raw}")
        proposals = _extract_json(raw)
    except Exception as e:
        log.error(f"Taxonomy review failed for {parent}/{subcategory}: {e}")
        return []
    # Before the guardrails, so size and share are judged on the set that would
    # actually move rather than the subset the model happened to write down.
    for p in proposals:
        if isinstance(p, dict) and p.get("evidence_ids"):
            p["evidence_ids"] = expand_evidence(p["evidence_ids"], rows)
    filtered = filter_proposals(
        proposals, source_size=len(rows), valid_ids={r["id"] for r in rows},
        taxonomy=taxonomy, source_parent=parent, source_sub=subcategory,
        blocked_names=blocked_names,
    )
    for p in filtered:
        p["parent_name"] = parent
        p["source_sub"] = subcategory
    return filtered


# ── Persistence ───────────────────────────────────────────────────────────────

def store_proposals(proposals: list[dict], key: str) -> list[dict]:
    """Write this month's proposals, whole -- the server sends the card for any
    pending proposal it hasn't sent yet. Anything still undecided from an
    earlier month is superseded by these, so it expires."""
    con = get_con()
    con.execute("UPDATE taxonomy_proposals SET status = 'expired' WHERE status = 'pending'")
    stored = []
    for p in proposals:
        proposal_id = insert_with_next_id(
            con,
            """INSERT INTO taxonomy_proposals
               (id, parent_name, source_sub, action, target_parent, proposed_sub,
                rationale, evidence_ids, examples, status, proposed_at, run_key)
               VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM taxonomy_proposals), %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
               RETURNING id""",
            [p["parent_name"], p["source_sub"], p["action"], p["target_parent"],
             p["target_sub"], p["rationale"], json.dumps(p["evidence_ids"]),
             json.dumps(p.get("examples", [])), now(), key],
            "taxonomy_proposals",
        )
        stored.append({**p, "id": proposal_id})
    return stored


def _record_empty_run(key: str) -> None:
    """A month where nothing met the bar still has to be recorded, or the review
    re-runs on every pipeline pass for the rest of the month. Stored as an
    already-expired sentinel row so it never reaches Telegram."""
    con = get_con()
    insert_with_next_id(
        con,
        """INSERT INTO taxonomy_proposals
           (id, parent_name, source_sub, action, target_parent, proposed_sub,
            rationale, evidence_ids, status, proposed_at, run_key)
           VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM taxonomy_proposals), '—', '—', 'create', '—', %s,
                   'No clusters met the guardrails.', '[]', 'expired', %s, %s) RETURNING id""",
        [f"(no proposals {key})", now(), key], "taxonomy_proposals",
    )


def apply_approved(proposal: dict) -> int:
    """Create the subcategory and move exactly the transactions whose ids were
    shown on the card. Returns how many are now under it.

    Re-checks each transaction is still where it was: the user may have
    recategorised some by hand in the dashboard between the card being sent and
    approved, and an approval should never override a later manual decision."""
    from database_functions import update_classification, upsert_parent, upsert_subcategory

    con = get_con()
    ids = proposal["evidence_ids"]
    if not ids:
        return 0
    # A "move" targets a category that already exists, so target_parent may
    # differ from the source parent -- the label has to name both, or the
    # transaction ends up with a subcategory that doesn't belong to its parent
    # and disappears from every category-based view.
    target_parent = proposal.get("target_parent") or proposal["parent_name"]
    target_sub = proposal["proposed_sub"]
    if proposal.get("action", "create") == "create":
        upsert_subcategory(target_sub, upsert_parent(target_parent))

    placeholders = ", ".join("%s" for _ in ids)
    still_there = [r[0] for r in con.execute(
        f"""SELECT id FROM transactions
            WHERE id IN ({placeholders}) AND llm_category = %s AND llm_subcategory = %s""",
        [*ids, proposal["parent_name"], proposal["source_sub"]],
    ).fetchall()]
    for txn_id in still_there:
        update_classification(txn_id, target_parent, target_sub, None, "taxonomy-proposal")
    count = con.execute(
        "SELECT COUNT(*) FROM transactions WHERE llm_category = %s AND llm_subcategory = %s",
        [target_parent, target_sub],
    ).fetchone()[0]
    log.info(f"Applied '{target_sub}' — {count} transaction(s) now under it")
    return count


def collect_decisions() -> int:
    """Apply what was approved on Telegram since the last run (the server
    records the decision on the proposal itself). Returns how many were applied.
    A denial needs nothing doing: it's already recorded, and it keeps that name
    from being proposed again."""
    con = get_con()
    applied = 0
    for row in con.execute(
        """SELECT id, parent_name, source_sub, action, target_parent, proposed_sub, evidence_ids
           FROM taxonomy_proposals WHERE status = 'approved' ORDER BY id"""
    ).fetchall():
        proposal = dict(zip(
            ("id", "parent_name", "source_sub", "action", "target_parent", "proposed_sub", "evidence_ids"), row))
        try:
            apply_approved(proposal)
            con.execute("UPDATE taxonomy_proposals SET status = 'applied', applied_at = %s WHERE id = %s",
                        [now(), proposal["id"]])
            applied += 1
        except Exception as e:
            log.error(f"Failed to apply taxonomy decision {proposal['id']}: {e}", exc_info=True)
    return applied


def run(today: date | None = None) -> list[dict]:
    """Review once per calendar month. Returns the proposals sent, if any."""
    key = run_key(today)
    init_db()
    if already_reviewed(key):
        log.info(f"Taxonomy review already run for {key} — skipping")
        return []
    if not CLAUDE_SECRET:
        log.error("CLAUDE_SECRET not set — skipping taxonomy review")
        return []
    run_start = time.time()
    log.info(f"--- Taxonomy review started ({key}) ---")
    client = anthropic.Anthropic(api_key=CLAUDE_SECRET)
    taxonomy = full_taxonomy()
    # Names already proposed (approved OR denied) are off the table, so a
    # rejected idea never comes back a month later. Folded into the taxonomy so
    # a "create" collides with them exactly as it would an existing category.
    denied = previously_proposed_names()
    found: list[dict] = []

    candidates = reviewable_subcategories()
    # One API call per subcategory, so say how many are coming -- otherwise the
    # only sign of this stage in the log is a burst of bare httpx lines from the
    # SDK, which looks like a runaway loop rather than a bounded monthly job.
    log.info(
        f"Reviewing {len(candidates)} subcategories with at least "
        f"{MIN_SUBCATEGORY_SIZE} transactions (one request each)"
    )
    for i, sub in enumerate(candidates, 1):
        rows = transactions_in(sub["parent"], sub["subcategory"])
        if len(rows) < MIN_SUBCATEGORY_SIZE:
            continue
        t0 = time.time()
        proposals = review_subcategory(client, sub["parent"], sub["subcategory"],
                                       rows, taxonomy, blocked_names=denied)
        log.info(
            f"  [{i}/{len(candidates)}] {sub['subcategory']} ({len(rows)} transactions, "
            f"{time.time() - t0:.1f}s) — "
            + (", ".join(f"{p['action']} '{p['target_sub']}' ({p['evidence_count']})"
                         for p in proposals) if proposals else "nothing to propose")
        )
        for p in proposals:
            p["examples"] = [
                r["context"] for r in rows
                if r["id"] in p["evidence_ids"] and r["context"]
            ][:4]
            found.append(p)
            denied.add(p["target_sub"].lower())

    if not found:
        log.info(
            f"--- Taxonomy review complete in {time.time() - run_start:.1f}s "
            f"— nothing clear enough to propose, next review in {key[:4]}-"
            f"{int(key[5:]) % 12 + 1:02d} ---"
        )
        _record_empty_run(key)
        return []

    found.sort(key=lambda p: p["evidence_count"], reverse=True)
    found = found[:MAX_PROPOSALS_PER_RUN]
    stored = store_proposals(found, key)
    log.info(
        f"--- Taxonomy review complete in {time.time() - run_start:.1f}s "
        f"— {len(stored)} proposal(s); the server sends their cards ---"
    )
    return stored
