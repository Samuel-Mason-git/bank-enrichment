import asyncio
import json
import logging
import time

from server_db import get_con, get_quick_categories

log = logging.getLogger(__name__)

# (request_count when due, seconds since last_requested_at)
# At request_count=4, no further message is sent — transaction is auto-skipped.
FOLLOW_UP_SCHEDULE = [
    (1, 3_600),          # 1 hour  after initial send
    (2, 86_400),         # 1 day   after first follow-up
    (3, 86_400 * 2),     # 2 days  after second follow-up
    (4, 86_400 * 7),     # 1 week  → auto-skip
]

FOLLOW_UP_LABELS = {1: "1 hour", 2: "1 day", 3: "2 days"}


def run_requester(bot) -> None:
    con = get_con()

    # Handle transactions where the initial send never happened (bot was down, send failed, etc.)
    # Use received_at as the reference since last_requested_at will be NULL
    missed_cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 86_400))
    missed = con.execute(
        """SELECT id, payload FROM webhook_queue
           WHERE status = 'pending'
           AND skipped = FALSE
           AND request_count = 0
           AND received_at <= ?
           LIMIT 1""",
        [missed_cutoff]
    ).fetchall()

    for row in missed:
        transaction_id, payload_str = row[0], row[1]
        try:
            data = json.loads(payload_str)
            merchant_name = ((data.get('data') or {}).get('merchant') or {}).get('name')
            try:
                quick_categories = get_quick_categories(merchant_name)
            except Exception as e:
                log.warning(f"Failed to fetch quick categories for {transaction_id}, sending without them: {e}")
                quick_categories = []
            log.info(f"Missed-send recovery for {transaction_id}: merchant={merchant_name!r}, quick_categories={quick_categories}")
            bot.send_card(data, quick_categories=quick_categories)
            con.execute(
                "UPDATE webhook_queue SET request_count = 1, last_requested_at = ? WHERE id = ?",
                [time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
            )
            con.execute("UPDATE stats SET requests_sent = requests_sent + 1 WHERE id = 1")
            log.info(f"Missed initial send recovered for {transaction_id}")
            return
        except Exception as e:
            log.error(f"Missed send recovery failed for {transaction_id}: {e}", exc_info=True)

    for request_count, delay_seconds in FOLLOW_UP_SCHEDULE:
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - delay_seconds))
        rows = con.execute(
            """SELECT id, payload FROM webhook_queue
               WHERE status = 'pending'
               AND skipped = FALSE
               AND request_count = ?
               AND last_requested_at <= ?
               LIMIT 1""",
            [request_count, cutoff]
        ).fetchall()

        for row in rows:
            transaction_id, payload_str = row[0], row[1]

            if request_count == 4:
                try:
                    con.execute(
                        "UPDATE webhook_queue SET skipped = TRUE, status = 'enriched', user_context = 'Auto-skipped', enriched_at = ? WHERE id = ?",
                        [time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
                    )
                    log.info(f"Transaction auto-skipped after no response: {transaction_id}")
                except Exception as e:
                    log.error(f"Auto-skip failed for {transaction_id}: {e}", exc_info=True)
            else:
                try:
                    data = json.loads(payload_str)
                    merchant_name = ((data.get('data') or {}).get('merchant') or {}).get('name')
                    try:
                        quick_categories = get_quick_categories(merchant_name)
                    except Exception as e:
                        log.warning(f"Failed to fetch quick categories for {transaction_id}, sending without them: {e}")
                        quick_categories = []
                    log.info(f"Follow-up {request_count} for {transaction_id}: merchant={merchant_name!r}, quick_categories={quick_categories}")
                    bot.send_card(data, follow_up=request_count, quick_categories=quick_categories)
                    con.execute(
                        "UPDATE webhook_queue SET request_count = request_count + 1, last_requested_at = ? WHERE id = ?",
                        [time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
                    )
                    con.execute("UPDATE stats SET requests_sent = requests_sent + 1 WHERE id = 1")
                    log.info(f"Follow-up {request_count} sent for {transaction_id}")
                    return
                except Exception as e:
                    log.error(f"Follow-up {request_count} failed for {transaction_id}: {e}", exc_info=True)


# Taxonomy proposals are not urgent and the monthly cadence means a nudge that
# arrives too fast reads as nagging about something the user consciously left.
# Two gentle reminders, then it stops asking and the proposal simply waits --
# an undecided proposal is cleared by the next month's run, never auto-applied.
TAXONOMY_FOLLOW_UPS = [
    (0, 86_400 * 3),   # 3 days after the cards were sent
    (1, 86_400 * 7),   # a week after that
]


def run_taxonomy_follow_ups(bot) -> None:
    """Re-send the intro card for proposals still awaiting a decision."""
    import os
    con = get_con()
    for follow_up_count, delay_seconds in TAXONOMY_FOLLOW_UPS:
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - delay_seconds))
        rows = con.execute(
            """SELECT local_id, parent_name, source_sub, proposed_sub, rationale,
                      evidence_count, examples, action, target_parent
               FROM taxonomy_proposals
               WHERE status = 'pending' AND follow_up_count = ?
                 AND COALESCE(last_nudged_at, sent_at) <= ?""",
            [follow_up_count, cutoff]
        ).fetchall()
        if not rows:
            continue
        try:
            chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
            bot.send_taxonomy_intro(chat_id, len(rows), follow_up=follow_up_count + 1)
            for r in rows:
                bot.send_taxonomy_proposal(chat_id, {
                    "local_id": r[0], "parent_name": r[1], "source_sub": r[2],
                    "proposed_sub": r[3], "rationale": r[4], "evidence_count": r[5],
                    "examples": json.loads(r[6] or "[]"),
                    "action": r[7], "target_parent": r[8],
                })
            con.execute(
                """UPDATE taxonomy_proposals
                   SET follow_up_count = follow_up_count + 1, last_nudged_at = ?
                   WHERE status = 'pending' AND follow_up_count = ?""",
                [time.strftime("%Y-%m-%d %H:%M:%S"), follow_up_count]
            )
            log.info(f"Taxonomy follow-up {follow_up_count + 1} sent for {len(rows)} proposal(s)")
        except Exception as e:
            log.error(f"Taxonomy follow-up {follow_up_count} failed: {e}", exc_info=True)
        return


def run_cleanup() -> None:
    con = get_con()
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 86_400 * 5))
    deleted = con.execute(
        "DELETE FROM webhook_queue WHERE status = 'processed' AND enriched_at <= ? RETURNING id",
        [cutoff]
    ).fetchall()
    if deleted:
        log.info(f"Cleaned up {len(deleted)} processed transactions older than 5 days")


async def requester_loop(bot) -> None:
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            run_requester(bot)
        except Exception as e:
            log.error(f"Requester loop error: {e}", exc_info=True)
        try:
            run_taxonomy_follow_ups(bot)
        except Exception as e:
            log.error(f"Taxonomy follow-up loop error: {e}", exc_info=True)
        try:
            run_cleanup()
        except Exception as e:
            log.error(f"Cleanup loop error: {e}", exc_info=True)
