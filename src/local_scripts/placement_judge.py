"""A second opinion on where the classifier put a transaction.

The novelty gate in llm_labelling.py only fires when a placement needs a NEW
category name. A placement into an EXISTING one that is confidently wrong sails
straight through -- a one-off £282 holding deposit filed under Rent, a dentist
tapped into Alcohol. Nothing in Pass 0/1/2 can say "none of these really fit",
and asking the classifier to doubt itself just makes it doubt everything.

So a separate call does the doubting. It sees the placement, the amounts and
rhythm of what that subcategory already holds, and what else the same merchant
has been filed under, and either approves or objects. An objection does not
change anything by itself -- the transaction is held behind the same Telegram
card the novelty gate uses, offering the judge's suggestion next to "keep it
where it was", and the user decides.

The judge is told what the categories are FOR (aggregating spending over time),
because without that it nitpicks neighbouring subcategories that don't matter.

It fails CLOSED. A judge that quietly steps aside whenever the API hiccups (or
the balance runs out) is a check that only works when nothing is wrong, and the
transactions it skipped would be saved unreviewed with nothing to say so. So if
the judge is switched on but cannot reach a verdict, the transaction is left
unclassified and simply retried next run. The deliberate ways to turn it off --
PLACEMENT_JUDGE=off, no API key, a server that can't show a card -- pass
placements straight through as they always did, and are the release valve if it
ever needs to stop holding things back.
"""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import anthropic

import alerts
import category_proposals
from database_functions import (
    get_con, get_parents, get_subcategories, get_subcategory_examples,
    get_subcategory_stats, get_merchant_history,
)

log = logging.getLogger(__name__)

# Kill switch that doesn't need a code change: PLACEMENT_JUDGE=off in config/.env.
JUDGE_ENABLED = os.getenv("PLACEMENT_JUDGE", "on").strip().lower() != "off"
# Measured on 150 random transactions: the judge scores nearly every objection
# 0.82 and the genuine catches sit at 0.82-0.99, so this is effectively "did it
# object at all". A 0.9 cutoff would have missed the holding deposit.
MIN_CONFIDENCE = 0.8
MAX_WORKERS = 8
MAX_TOKENS = 600
# Longest rationale printed under an option on the Telegram card.
MAX_RATIONALE = 240


def merchant_key(txn: dict) -> str | None:
    """What identifies 'the same merchant' across transactions, for both the
    keep-it memory and the merchant history. None for a transaction with
    neither field -- there is nothing to generalise from."""
    name = txn.get("merchant_name") or txn.get("counterparty_name")
    return name.strip().lower() if name and name.strip() else None


# ── Prompt ─────────────────────────────────────────────────────────────────────

_VERDICT_SHAPE = (
    'Respond ONLY with a JSON object: {"verdict": "approve"|"reject", "confidence": 0-1, '
    '"issue": "none"|"wrong_area"|"distorts_totals"|"deserves_own_category", "reason": "<one sentence>", '
    '"suggested_parent": <string|null>, "suggested_subcategory": <string|null>, "suggested_is_new": <bool>}'
)


def _profile_block(stat: dict | None, examples: list[dict]) -> str:
    if not stat:
        return "  (no past transactions in this subcategory)"
    lines = [f"  {stat['count']} past transactions; amounts £{stat['min']:.2f}-£{stat['max']:.2f} (median £{stat['median']:.2f})"]
    if stat["gap_days"] is not None:
        lines.append(f"  median gap between transactions: {stat['gap_days']:.0f} days")
    lines.append("  Most recent examples:")
    for e in examples:
        bits = [f"£{abs(float(e['amount'])):.2f}", str(e["created_at"])[:10]]
        for key in ("merchant_name", "counterparty_name", "description", "user_context"):
            if e.get(key):
                bits.append(f"{key}={e[key]}")
        lines.append("    - " + " | ".join(bits))
    return "\n".join(lines)


def _merchant_block(history: list[dict]) -> str:
    if not history:
        return "  (no other transactions seen from this merchant/counterparty)"
    placed: dict[str, int] = {}
    for h in history:
        label = f"{h['llm_category']} > {h['llm_subcategory']}"
        placed[label] = placed.get(label, 0) + 1
    amounts = [h["amount"] for h in history]
    top = "; ".join(f"{label} ×{n}" for label, n in sorted(placed.items(), key=lambda kv: -kv[1])[:4])
    return (f"  {len(history)} other transaction(s), amounts £{min(amounts):.2f}-£{max(amounts):.2f}; "
            f"previously placed: {top}")


def _taxonomy_block() -> str:
    subs_by_parent: dict[str, list[str]] = {}
    for s in get_subcategories():
        subs_by_parent.setdefault(s["parent_name"], []).append(s["name"])
    return "\n".join(
        f"  {p['name']}: {', '.join(sorted(subs_by_parent.get(p['name'], []))) or '(none)'}"
        for p in sorted(get_parents(), key=lambda p: p["name"])
    )


def build_prompt(txn_text: str, category: str, subcategory: str, profile: str, merchant: str, taxonomy: str) -> str:
    return f"""You review how an automated system categorised one bank transaction, for a personal budgeting tool.

Purpose of the categories: the user aggregates spending by parent category and subcategory to see a detailed picture of their finances over time. Categories are allowed to overlap and do not need to be perfectly clean — a reasonable placement is fine. What matters is (a) whether the placement would mislead the aggregated picture, and (b) whether this transaction is something distinct enough that the user would want to track it on its own.

Transaction:
{txn_text}

Proposed placement: {category} > {subcategory}

What this subcategory currently holds (excluding this transaction):
{profile}

Other transactions from the same merchant/counterparty:
{merchant}

Full taxonomy:
{taxonomy}

Decide APPROVE or REJECT.

APPROVE when the placement is reasonable, even if a neighbouring subcategory would also work. Do NOT reject over: fine distinctions between neighbouring subcategories (breakfast vs lunch, café vs restaurant, pharmacy vs toiletries, and similar), small one-off amounts, or anything the user's own context plausibly explains — treat the user's context as authoritative about what the purchase actually was.

REJECT only for one of these reasons (put it in "issue"):
1. wrong_area — it clearly belongs under a different parent category or a different kind of money flow (income vs refund vs transfer vs spending), so it would land in the wrong part of the picture.
2. distorts_totals — its amount or nature is far out of line with what the subcategory holds (for example a one-off large payment beside small routine ones, or a one-off beside a recurring bill), so it would skew that subcategory's totals or rhythm.
3. deserves_own_category — it is a distinct kind of spend or income that matters enough to track separately: the amount is material, or the evidence (including the other transactions from this merchant/counterparty) suggests it recurs, and nothing in the taxonomy captures it. Never use this for trivial one-offs.

If you reject, suggest the better placement: an existing one from the taxonomy if it fits reasonably well; otherwise a NEW subcategory (only when reason 3 applies or nothing existing fits), with a concise name.
{_VERDICT_SHAPE}"""


def parse_verdict(raw: str) -> dict:
    return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])


def is_objection(verdict: dict, category: str, subcategory: str) -> bool:
    """A usable objection: a reject at or above MIN_CONFIDENCE that names a
    different placement to offer. An objection with no alternative, or one that
    'suggests' the placement it is objecting to (the judge does this when its
    real complaint is the amount), gives the user nothing to choose between, so
    the placement stands."""
    if verdict.get("verdict") != "reject":
        return False
    try:
        if float(verdict.get("confidence") or 0) < MIN_CONFIDENCE:
            return False
    except (TypeError, ValueError):
        return False
    parent = (verdict.get("suggested_parent") or "").strip()
    sub = (verdict.get("suggested_subcategory") or "").strip()
    if not parent or not sub:
        return False
    return (parent.lower(), sub.lower()) != (category.strip().lower(), subcategory.strip().lower())


def hold_options(category: str, subcategory: str, verdict: dict, existing_parents: set[str]) -> list[dict]:
    """The card's two choices. 'judge' marks the card as a second-look card so
    its option names never feed the classifier's declined-names list (see
    category_proposals._denied_options), and 'is_original' marks the choice
    that means 'the placement was fine' -- both stay in the local database,
    since the server only stores the fields it declares."""
    parent = verdict["suggested_parent"].strip()
    sub = verdict["suggested_subcategory"].strip()
    reason = " ".join(str(verdict.get("reason") or "").split())
    if len(reason) > MAX_RATIONALE:
        reason = reason[:MAX_RATIONALE - 1].rstrip() + "…"
    return [
        {
            "parent_name": parent, "subcategory_name": sub,
            "parent_is_new": parent.lower() not in existing_parents,
            "rationale": f"Second look: {reason}" if reason else "Second look: this looks like a better fit.",
            "judge": True,
        },
        {
            "parent_name": category, "subcategory_name": subcategory, "parent_is_new": False,
            "rationale": "Keep it where it was.", "judge": True, "is_original": True,
        },
    ]


# ── Reviewer ───────────────────────────────────────────────────────────────────

@dataclass
class ReviewResult:
    """objections: {transaction id: verdict} for placements the judge questioned.
    unavailable: ids the judge was supposed to review but could not reach a
    verdict on -- the caller must NOT save these. Anything in neither was
    approved, already settled, or not subject to review at all."""
    objections: dict[str, dict] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)


class PlacementReviewer:
    """Reviews placements and holds the ones it objects to. One instance per
    run: it remembers which proposals it created so the caller can sync them,
    and stops calling the API for the rest of the run on a credit error rather
    than failing the same call for every remaining transaction."""

    def __init__(self, client, model: str, format_txn, gate_check=lambda: True):
        self.client = client
        self.model = model
        self.format_txn = format_txn
        # Evaluated lazily and once: it is an HTTP round trip, and process.py
        # builds a reviewer on every run even when there are no quick-taps.
        self._gate_check = gate_check
        self._gate_ok = None
        self._out_of_credit = False
        self.new_proposal_ids: list[int] = []
        # Placements left unsaved this run because the judge could not answer.
        self.unavailable_count = 0

    @property
    def configured(self) -> bool:
        """Whether review is meant to happen at all. False is a deliberate
        choice (switched off, no key, no way to show a card), so placements
        pass through untouched -- unlike a judge that is configured but failing."""
        if not JUDGE_ENABLED or self.client is None:
            return False
        if self._gate_ok is None:
            self._gate_ok = bool(self._gate_check())
        return self._gate_ok

    # -- memory --

    def _already_settled(self, txn: dict, category: str, subcategory: str) -> bool:
        con = get_con()
        if con.execute("SELECT 1 FROM judge_reviews WHERE txn_id = ?", [txn["id"]]).fetchone():
            return True
        key = merchant_key(txn)
        if key and con.execute(
            """SELECT 1 FROM judge_reviews
               WHERE merchant_key = ? AND category = ? AND subcategory = ? AND outcome = 'kept'""",
            [key, category, subcategory],
        ).fetchone():
            return True
        return False

    def _record(self, txn: dict, category: str, subcategory: str, outcome: str) -> None:
        get_con().execute(
            """INSERT OR REPLACE INTO judge_reviews (txn_id, merchant_key, category, subcategory, outcome, reviewed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [txn["id"], merchant_key(txn), category, subcategory, outcome, time.strftime("%Y-%m-%d %H:%M:%S")],
        )

    # -- review --

    def _call(self, prompt: str) -> dict | None:
        if self._out_of_credit:
            return None
        try:
            response = self.client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, messages=[{"role": "user", "content": prompt}]
            )
            return parse_verdict(response.content[0].text)
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e):
                self._out_of_credit = True
            else:
                log.warning(f"Placement judge call failed: {e}")
            alerts.llm_failure("the placement judge", e)
        except Exception as e:
            log.warning(f"Placement judge call failed: {e}")
            alerts.llm_failure("the placement judge", e)
        return None

    def review(self, items: list[tuple[dict, str, str]]) -> ReviewResult:
        """items are (transaction, category, subcategory) placements about to be
        written. See ReviewResult for what comes back."""
        result = ReviewResult()
        if not items or not self.configured:
            return result

        stats = get_subcategory_stats()
        taxonomy = _taxonomy_block()
        todo, prompts = [], []
        for txn, category, subcategory in items:
            if not category or not subcategory or self._already_settled(txn, category, subcategory):
                continue
            key = merchant_key(txn)
            prompts.append(build_prompt(
                self.format_txn(txn), category, subcategory,
                _profile_block(stats.get((category, subcategory)), get_subcategory_examples(category, subcategory)),
                _merchant_block(get_merchant_history(key, txn["id"]) if key else []),
                taxonomy,
            ))
            todo.append((txn, category, subcategory))

        # The API calls are the slow part and touch no shared state, so they
        # run concurrently. Everything above and below stays on this thread,
        # where the DuckDB connection lives.
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            verdicts = list(pool.map(self._call, prompts))

        for (txn, category, subcategory), verdict in zip(todo, verdicts):
            if verdict is None:
                result.unavailable.add(txn["id"])
            elif is_objection(verdict, category, subcategory):
                result.objections[txn["id"]] = verdict
            else:
                self._record(txn, category, subcategory, "approved")

        if self._out_of_credit:
            log.error("Placement judge cannot run: the Anthropic credit balance is too low")
        if result.unavailable:
            self.unavailable_count += len(result.unavailable)
            log.error(
                f"Placement judge gave no verdict for {len(result.unavailable)}/{len(todo)} placement(s) — "
                f"leaving them unclassified to retry next run (set PLACEMENT_JUDGE=off in config/.env to skip the check)"
            )
            alerts.send_alert(
                "judge-held-back", "Placement judge is holding transactions back",
                f"{len(result.unavailable)} transaction(s) were left unclassified because the placement judge couldn't "
                f"review them. They're retried on the next run. To skip the check instead, set PLACEMENT_JUDGE=off in config/.env.",
            )
        return result

    # -- hold --

    def hold(self, objected: list[tuple[dict, str, str, dict]]) -> int:
        """Lock each objected placement behind a card. (transaction, category,
        subcategory, verdict) in; returns how many transactions were held. New
        proposal ids accumulate on self.new_proposal_ids for the caller to sync."""
        existing_parents = {p["name"].strip().lower() for p in get_parents()}
        groups: dict[tuple, dict] = {}
        for txn, category, subcategory, verdict in objected:
            key = (
                verdict["suggested_parent"].strip().lower(), verdict["suggested_subcategory"].strip().lower(),
                category.lower(), subcategory.lower(),
            )
            group = groups.setdefault(key, {"txns": [], "options": hold_options(category, subcategory, verdict, existing_parents)})
            group["txns"].append((txn, category, subcategory))

        held = 0
        for group in groups.values():
            ids = [t["id"] for t, _, _ in group["txns"]]
            proposal_id, is_new = category_proposals.register_group(group["options"], ids)
            if is_new:
                self.new_proposal_ids.append(proposal_id)
            for txn, category, subcategory in group["txns"]:
                self._record(txn, category, subcategory, "held")
            held += len(ids)
            log.info(
                f"  [JUDGE-HOLD] {len(ids)} transaction(s) — '{group['txns'][0][1]} / {group['txns'][0][2]}' questioned, "
                f"suggested '{group['options'][0]['parent_name']} / {group['options'][0]['subcategory_name']}'"
            )
        return held

    def review_tap(self, txn: dict, category: str, subcategory: str) -> bool:
        """The hook apply_quick_tap_classifications() calls for each tap.
        True means the tap must not be applied now: either the judge objected
        and the transaction is behind a card, or it could not answer and the
        tap is simply retried next run."""
        result = self.review([(txn, category, subcategory)])
        if txn["id"] in result.unavailable:
            return True
        if txn["id"] not in result.objections:
            return False
        self.hold([(txn, category, subcategory, result.objections[txn["id"]])])
        return True

    def sync(self) -> None:
        """Send the cards for every proposal this run created."""
        if not self.new_proposal_ids:
            return
        try:
            category_proposals.sync_new_proposals(self.new_proposal_ids)
        except Exception as e:
            log.error(f"Failed to sync placement-judge proposal(s) to the server: {e}", exc_info=True)
        self.new_proposal_ids = []
