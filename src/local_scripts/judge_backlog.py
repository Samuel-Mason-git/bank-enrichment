"""Give transactions classified BEFORE the placement judge existed the same second look.

The judge (placement_judge.py) reviews new placements as they are made, but everything
already in the database went through without it. This runs the judge over that history
and holds the placements it objects to behind the usual Telegram cards -- suggestion next
to "keep it where it was".

Nothing changes until you tap a card. A held transaction stays classified where it is, so
your totals don't move while a card is waiting, and declining or ignoring one leaves it
exactly as it was. Choosing the suggestion reclassifies it, and choosing "keep" is
remembered so the same merchant and placement isn't questioned again.

Judging is one paid API call per transaction, so by default this only counts them and
estimates the cost. Nothing is sent to Claude, written, or held without --yes:

    python src/local_scripts/judge_backlog.py                 # how many, roughly what it costs
    python src/local_scripts/judge_backlog.py --yes           # judge them, send up to 10 cards
    python src/local_scripts/judge_backlog.py --yes --cards 5 # fewer cards at a time

Every transaction is judged once, and its verdict recorded, so re-running only reviews what
is left (including objections that didn't get a card because of --cards). Transactions you
classified yourself (dashboard edits, card choices) are never second-guessed.
"""
import argparse
import logging
import sys

import duckdb

import placement_judge
from database_functions import _rows, backup_db, get_con, init_db
from llm_labelling import make_placement_reviewer

# Measured on 342 judge calls (Sep 2026): 449k input + 39k output tokens, about $1.93 at
# Sonnet pricing. An estimate for planning, not a quote.
COST_PER_CALL = 0.0056
CHUNK = 40
# Classifications that are the user's own decision.
USER_DECISIONS = ("manual", "category-proposal")

log = logging.getLogger(__name__)


def _has_judge_table() -> bool:
    return get_con().execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'judge_reviews'"
    ).fetchone()[0] > 0


def candidates() -> list[dict]:
    """Classified transactions the judge hasn't looked at yet, newest first."""
    already_judged = (
        "AND NOT EXISTS (SELECT 1 FROM judge_reviews j WHERE j.txn_id = t.id)" if _has_judge_table() else ""
    )
    placeholders = ", ".join("?" for _ in USER_DECISIONS)
    return _rows(
        f"""SELECT * FROM transactions t
            WHERE llm_category IS NOT NULL AND llm_subcategory IS NOT NULL
            AND skipped = FALSE AND pending_category_proposal_id IS NULL
            AND COALESCE(llm_model, '') NOT IN ({placeholders})
            {already_judged}
            ORDER BY created_at DESC""",
        list(USER_DECISIONS),
    )


def choose_groups(objections: list[tuple], max_cards: int) -> tuple[list[tuple], list[tuple]]:
    """Split objections into those going on a card now and those left for a later run.
    Objections that would share a card count once, and the most confident cards go first."""
    groups: dict[tuple, list[tuple]] = {}
    for item in objections:
        groups.setdefault(placement_judge.group_key(item[1], item[2], item[3]), []).append(item)
    ranked = sorted(groups.values(), key=lambda g: -max(float(i[3].get("confidence") or 0) for i in g))
    return [i for g in ranked[:max_cards] for i in g], [i for g in ranked[max_cards:] for i in g]


def describe(item: tuple) -> str:
    txn, category, subcategory, verdict = item
    who = txn.get("merchant_name") or txn.get("counterparty_name") or txn.get("description") or ""
    context = f" | ctx: {txn['user_context']}" if txn.get("user_context") else ""
    reason = " ".join(str(verdict.get("reason") or "").split())[:150]
    return (
        f"  {float(verdict.get('confidence') or 0):.2f}  {str(txn.get('created_at'))[:10]}  £{float(txn['amount']):>9.2f}  "
        f"{who[:32]}{context}\n"
        f"        {category} > {subcategory}  ->  {verdict['suggested_parent']} > {verdict['suggested_subcategory']}\n"
        f"        {reason}"
    )


def parse_args(argv):
    p = argparse.ArgumentParser(description="Second-look review of transactions classified before the judge existed.")
    p.add_argument("--yes", action="store_true", help="actually judge them (costs API credit) and hold objections behind cards")
    p.add_argument("--cards", type=int, default=10, help="most new Telegram cards to send this run (default 10)")
    p.add_argument("--limit", type=int, help="judge at most this many transactions, newest first")
    p.add_argument("--list-only", action="store_true", help="with --yes: judge and show the objections, but send no cards")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        init_db(read_only=not args.yes)
    except duckdb.IOException as e:
        print(f"Can't open the database -- is the Streamlit dashboard (or process.py) running? Close it and retry.\n  {e}",
              file=sys.stderr)
        return 1

    todo = candidates()
    if args.limit:
        todo = todo[:args.limit]

    if not args.yes:
        print(f"{len(todo)} classified transaction(s) haven't had a second look yet.")
        print(f"Judging them is {len(todo)} API call(s), roughly ${len(todo) * COST_PER_CALL:.2f} "
              f"(an estimate from earlier runs, about ${COST_PER_CALL} a call).")
        print("Nothing has been sent, written or held. Re-run with --yes to go ahead.")
        return 0

    reviewer = make_placement_reviewer()
    if not reviewer.configured:
        print("The judge is switched off (PLACEMENT_JUDGE=off, no CLAUDE_SECRET, or the server can't show cards).",
              file=sys.stderr)
        return 1
    if not todo:
        print("Nothing left to review.")
        return 0

    backup_db("judge-backlog")
    print(f"Judging {len(todo)} transaction(s)...")
    objections: list[tuple] = []
    unavailable = 0
    for start in range(0, len(todo), CHUNK):
        chunk = todo[start:start + CHUNK]
        result = reviewer.review([(t, t["llm_category"], t["llm_subcategory"]) for t in chunk])
        unavailable += len(result.unavailable)
        objections += [
            (t, t["llm_category"], t["llm_subcategory"], result.objections[t["id"]])
            for t in chunk if t["id"] in result.objections
        ]
        print(f"  {min(start + CHUNK, len(todo))}/{len(todo)} judged, {len(objections)} objection(s) so far")
        if reviewer.out_of_credit:
            print("Stopped: the Anthropic credit balance is too low. Top up and re-run to continue where this left off.",
                  file=sys.stderr)
            break

    objections.sort(key=lambda i: -float(i[3].get("confidence") or 0))
    print(f"\n{len(objections)} placement(s) the judge questioned:\n")
    for item in objections:
        print(describe(item))

    held = new_cards = 0
    left_over = objections
    if objections and not args.list_only:
        to_hold, left_over = choose_groups(objections, args.cards)
        held = reviewer.hold(to_hold)
        new_cards = len(reviewer.new_proposal_ids)  # sync() clears it
        reviewer.sync()
    print(f"\n{held} transaction(s) held behind {new_cards} new card(s); nothing changes until you tap one.")
    if left_over and not args.list_only:
        print(f"{len(left_over)} more objection(s) are waiting: re-run with --yes to send the next batch of cards.")
    if unavailable:
        print(f"{unavailable} transaction(s) couldn't be judged this time and will be picked up on the next run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
