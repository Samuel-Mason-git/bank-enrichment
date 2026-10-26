import asyncio
import logging
import os
import time

from monzo_oauth import fetch_transaction
from server_db import get_con, get_quick_categories, now, record_context, record_request, record_update

log = logging.getLogger(__name__)

# (request_count when due, seconds since the last request)
# At request_count=4, no further message is sent — the transaction is auto-skipped.
FOLLOW_UP_SCHEDULE = [
    (1, 3_600),          # 1 hour  after initial send
    (2, 86_400),         # 1 day   after first follow-up
    (3, 86_400 * 2),     # 2 days  after second follow-up
    (4, 86_400 * 7),     # 1 week  → auto-skip
]

FOLLOW_UP_LABELS = {1: "1 hour", 2: "1 day", 3: "2 days"}


def _cutoff(seconds_ago: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - seconds_ago))


def _send(bot, con, transaction_id: str, payload: dict, follow_up: int = 0) -> None:
    # Falls back to counterparty for direct debits/bank transfers, which
    # rarely populate merchant -- otherwise recurring bills never get a
    # merchant-specific quick-tap suggestion, only the generic top-5.
    data = payload.get("data") or {}
    merchant_name = (data.get("merchant") or {}).get("name") or (data.get("counterparty") or {}).get("name")
    try:
        quick_categories = get_quick_categories(merchant_name)
    except Exception as e:
        log.warning(f"Failed to fetch quick categories for {transaction_id}, sending without them: {e}")
        quick_categories = []
    log.info(f"{'Follow-up ' + str(follow_up) if follow_up else 'Missed-send recovery'} for {transaction_id}: "
             f"merchant={merchant_name!r}, quick_categories={quick_categories}")
    bot.send_card(payload, follow_up=follow_up, quick_categories=quick_categories)
    record_request(con, transaction_id)


def run_requester(bot) -> None:
    """One card per call at most: the oldest one due."""
    con = get_con()

    # Transactions whose first card never went out (bot was down, send failed).
    # Not straight away -- the webhook may still be sending it.
    missed = con.execute(
        """SELECT n.id, c.payload FROM needs_context n JOIN transaction_current c ON c.id = n.id
           WHERE n.request_count = 0 AND n.received_at <= %s ORDER BY n.received_at LIMIT 1""",
        [_cutoff(MISSED_CARD_AFTER)]
    ).fetchone()
    if missed:
        try:
            _send(bot, con, missed[0], missed[1])
            log.info(f"Missed initial send recovered for {missed[0]}")
        except Exception as e:
            log.error(f"Missed send recovery failed for {missed[0]}: {e}", exc_info=True)
        return

    for request_count, delay_seconds in FOLLOW_UP_SCHEDULE:
        row = con.execute(
            """SELECT n.id, c.payload FROM needs_context n JOIN transaction_current c ON c.id = n.id
               WHERE n.request_count = %s AND n.last_requested_at <= %s
               ORDER BY n.last_requested_at LIMIT 1""",
            [request_count, _cutoff(delay_seconds)]
        ).fetchone()
        if not row:
            continue
        transaction_id, payload = row
        if request_count == 4:
            try:
                record_context(con, transaction_id, "Auto-skipped", skipped=True, source="timeout")
                log.info(f"Transaction auto-skipped after no response: {transaction_id}")
            except Exception as e:
                log.error(f"Auto-skip failed for {transaction_id}: {e}", exc_info=True)
        else:
            try:
                _send(bot, con, transaction_id, payload, follow_up=request_count)
                log.info(f"Follow-up {request_count} sent for {transaction_id}")
                return
            except Exception as e:
                log.error(f"Follow-up {request_count} failed for {transaction_id}: {e}", exc_info=True)


def _taxonomy_card(row) -> dict:
    proposal_id, parent_name, source_sub, proposed_sub, rationale, evidence_ids, examples, action, target_parent = row
    return {
        "local_id": proposal_id, "parent_name": parent_name, "source_sub": source_sub,
        "proposed_sub": proposed_sub, "rationale": rationale, "evidence_count": len(evidence_ids),
        "examples": examples, "action": action, "target_parent": target_parent,
    }


_TAXONOMY_COLUMNS = ("id, parent_name, source_sub, proposed_sub, rationale, evidence_ids, examples, "
                     "action, target_parent")


def run_new_cards(bot) -> None:
    """Send the card for every proposal and rule suggestion the pipeline has
    added since the last look -- those still pending that haven't been sent.
    sent_at is set only once a card has gone, so a failed send is retried."""
    con = get_con()
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))

    taxonomy = con.execute(
        f"SELECT {_TAXONOMY_COLUMNS} FROM taxonomy_proposals "
        "WHERE status = 'pending' AND sent_at IS NULL ORDER BY id"
    ).fetchall()
    if taxonomy:
        bot.send_taxonomy_intro(chat_id, len(taxonomy))
        for row in taxonomy:
            bot.send_taxonomy_proposal(chat_id, _taxonomy_card(row))
            con.execute("UPDATE taxonomy_proposals SET sent_at = %s WHERE id = %s", [now(), row[0]])
        log.info(f"Sent {len(taxonomy)} taxonomy proposal card(s)")

    for proposal_id, options, txn_ids, examples in con.execute(
        "SELECT id, options, txn_ids, examples FROM category_proposals "
        "WHERE status = 'pending' AND sent_at IS NULL ORDER BY id"
    ).fetchall():
        bot.send_category_proposal(chat_id, {
            "local_id": proposal_id, "options": options, "txn_count": len(txn_ids), "examples": examples,
        })
        con.execute("UPDATE category_proposals SET sent_at = %s WHERE id = %s", [now(), proposal_id])
        log.info(f"Sent category proposal card {proposal_id}")

    for row in con.execute(
        """SELECT id, match_field, match_value, category, subcategory, txn_count, auto_context, llm_reasoning
           FROM rule_suggestions WHERE status = 'pending' AND sent_at IS NULL ORDER BY id"""
    ).fetchall():
        bot.send_rule_suggestion(chat_id, dict(zip(
            ["id", "match_field", "match_value", "category", "subcategory", "txn_count", "auto_context",
             "llm_reasoning"], row)))
        con.execute("UPDATE rule_suggestions SET sent_at = %s WHERE id = %s", [now(), row[0]])
        log.info(f"Sent rule suggestion card {row[0]}")


# Taxonomy proposals are not urgent and the monthly cadence means a nudge that
# arrives too fast reads as nagging about something the user consciously left.
# Two gentle reminders, then it stops asking and the proposal simply waits --
# an undecided proposal is expired by the next month's run, never auto-applied.
# A first card that didn't go out is sent again this long after the payment
# arrived (long enough that the webhook isn't still sending it).
MISSED_CARD_AFTER = 600

TAXONOMY_FOLLOW_UPS = [
    (0, 86_400 * 3),   # 3 days after the cards were sent
    (1, 86_400 * 7),   # a week after that
]


def run_taxonomy_follow_ups(bot) -> None:
    """Re-send the cards for proposals still awaiting a decision."""
    con = get_con()
    for follow_up_count, delay_seconds in TAXONOMY_FOLLOW_UPS:
        rows = con.execute(
            f"""SELECT {_TAXONOMY_COLUMNS} FROM taxonomy_proposals
                WHERE status = 'pending' AND sent_at IS NOT NULL AND follow_up_count = %s
                  AND COALESCE(last_nudged_at, sent_at) <= %s ORDER BY id""",
            [follow_up_count, _cutoff(delay_seconds)]
        ).fetchall()
        if not rows:
            continue
        try:
            chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
            bot.send_taxonomy_intro(chat_id, len(rows), follow_up=follow_up_count + 1)
            for row in rows:
                bot.send_taxonomy_proposal(chat_id, _taxonomy_card(row))
            con.execute(
                """UPDATE taxonomy_proposals SET follow_up_count = follow_up_count + 1, last_nudged_at = %s
                   WHERE id = ANY(%s)""",
                [now(), [row[0] for row in rows]]
            )
            log.info(f"Taxonomy follow-up {follow_up_count + 1} sent for {len(rows)} proposal(s)")
        except Exception as e:
            log.error(f"Taxonomy follow-up {follow_up_count} failed: {e}", exc_info=True)
        return


# Card payments still without a merchant are looked up in Monzo's API again,
# this often, for this long after they arrived.
REPAIR_EVERY = 3_600
REPAIR_FOR = 86_400 * 7
_last_repair = 0.0


def run_merchant_repair(bot) -> None:
    """Card payments still without a merchant -- the webhook's own lookup
    failed, or Monzo hadn't filled it in yet -- looked up again, at most hourly,
    for a week after each arrived. Monzo's answer is kept as an update, merchant
    or not, and once there is one that payment isn't looked up again."""
    global _last_repair
    if time.time() - _last_repair < REPAIR_EVERY:
        return
    _last_repair = time.time()
    con = get_con()
    missing = con.execute(
        """SELECT t.id FROM transactions t
           WHERE t.merchant_name IS NULL AND COALESCE(t.counterparty_name, '') = ''
             AND t.raw_payload->'data'->>'scheme' = 'mastercard'
             AND t.received_at BETWEEN %s AND %s
             AND NOT EXISTS (SELECT 1 FROM transaction_updates u WHERE u.txn_id = t.id AND u.source = 'api')
           ORDER BY t.received_at DESC LIMIT 10""",
        [_cutoff(REPAIR_FOR), _cutoff(MISSED_CARD_AFTER)],
    ).fetchall()
    for (txn_id,) in missing:
        fetched = fetch_transaction(txn_id)
        if fetched is None:
            continue  # Monzo couldn't be asked: next hour
        record_update(con, txn_id, {"type": "transaction.updated", "data": fetched}, "api")
        merchant = (fetched.get("merchant") or {}).get("name") if isinstance(fetched.get("merchant"), dict) else None
        log.info(f"Merchant repair for {txn_id}: {merchant!r}" if merchant else
                 f"Merchant repair for {txn_id}: Monzo has none either")


async def requester_loop(bot) -> None:
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        for job in (run_new_cards, run_requester, run_taxonomy_follow_ups, run_merchant_repair):
            # In a thread: Telegram and the database can be slow, and retries
            # wait -- none of which may hold up a webhook arriving meanwhile.
            try:
                await asyncio.to_thread(job, bot)
            except Exception as e:
                log.error(f"{job.__name__} failed: {e}", exc_info=True)
