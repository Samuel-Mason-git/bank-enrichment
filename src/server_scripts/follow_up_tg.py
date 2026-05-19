import asyncio
import json
import logging
import time

from server_db import get_con

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
            bot.send_card(json.loads(payload_str))
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
                con.execute(
                    "UPDATE webhook_queue SET skipped = TRUE, status = 'enriched', user_context = 'Auto-skipped', enriched_at = ? WHERE id = ?",
                    [time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
                )
                log.info(f"Transaction auto-skipped after no response: {transaction_id}")
            else:
                try:
                    bot.send_card(json.loads(payload_str), follow_up=request_count)
                    con.execute(
                        "UPDATE webhook_queue SET request_count = request_count + 1, last_requested_at = ? WHERE id = ?",
                        [time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
                    )
                    con.execute("UPDATE stats SET requests_sent = requests_sent + 1 WHERE id = 1")
                    log.info(f"Follow-up {request_count} sent for {transaction_id}")
                    return
                except Exception as e:
                    log.error(f"Follow-up {request_count} failed for {transaction_id}: {e}", exc_info=True)


async def requester_loop(bot) -> None:
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            run_requester(bot)
        except Exception as e:
            log.error(f"Requester loop error: {e}", exc_info=True)
