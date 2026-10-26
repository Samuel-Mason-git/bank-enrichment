"""Runs the pipeline every morning, on the server (the `worker` container in
docker-compose.yml) -- so classification, proposals and summaries happen even
when the PC is off.

Sleeps until the next RUN_AT London time, runs process.py's steps, and
repeats. If the database can't be reached, it tries again every
RETRY_EVERY seconds, RETRIES times, before giving up for the day. A run that
can't start, or crashes, is logged and sent to Telegram, and the worker
carries on to the next morning. On Sundays it also sends a short "all fine",
so a worker that has quietly stopped doesn't go unnoticed for long.
`python worker.py --now` runs once straight away and exits, for trying it by
hand."""
import logging
import sys
import time
from datetime import datetime, timedelta, time as clock
from zoneinfo import ZoneInfo

import alerts
import process

RUN_AT = clock(8, 0)
LONDON = ZoneInfo("Europe/London")
RETRIES = 3
RETRY_EVERY = 30 * 60

log = logging.getLogger("worker")

# Waits between retries -- a name of its own, so tests can skip them.
_sleep = time.sleep


def next_run(now: datetime) -> datetime:
    """The next RUN_AT on London's clock, strictly after `now` (an aware
    datetime). Worked out on the wall clock, so it stays at 08:00 across the
    clocks going forward or back."""
    local = now.astimezone(LONDON)
    candidate = datetime.combine(local.date(), RUN_AT, tzinfo=LONDON)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), RUN_AT, tzinfo=LONDON)
    return candidate


def run_once(retries: int = RETRIES) -> int:
    alerts.start_run()
    try:
        code = process.main()
        for attempt in range(retries):
            if code != process.NO_DATABASE:
                break
            log.warning(f"Couldn't reach the database; trying again in {RETRY_EVERY // 60} minutes "
                        f"({attempt + 1}/{retries})")
            _sleep(RETRY_EVERY)
            code = process.main()
    except Exception as e:  # the steps catch their own; this is the run itself failing
        log.error(f"The daily run crashed: {e}", exc_info=True)
        alerts.send_alert("worker-crash", "The daily run crashed",
                          f"{type(e).__name__}. The worker carries on and tries again tomorrow at 08:00.")
        return 1
    if code == process.NO_DATABASE:
        alerts.send_alert("worker-not-started", "The daily run didn't start",
                          f"It couldn't reach the database, {retries + 1} times over "
                          f"{retries * RETRY_EVERY // 60} minutes. It tries again tomorrow at 08:00.")
    elif code == process.ALREADY_RUNNING:
        alerts.send_alert("worker-not-started", "The daily run didn't start",
                          "Another run was already going (one started by hand?). It runs again tomorrow at 08:00.")
    return code


def weekly_check_in() -> None:
    """Sunday's "all fine", with what's waiting on you."""
    from database_functions import get_con
    con = get_con()
    week = con.execute("SELECT COUNT(*) FROM transactions WHERE received_at >= NOW() - INTERVAL '7 days'").fetchone()[0]
    answer = con.execute("SELECT COUNT(*) FROM needs_context").fetchone()[0]
    classify = con.execute("SELECT COUNT(*) FROM needs_label").fetchone()[0]
    cards = con.execute(
        "SELECT (SELECT COUNT(*) FROM category_proposals WHERE status = 'pending') "
        "+ (SELECT COUNT(*) FROM taxonomy_proposals WHERE status = 'pending') "
        "+ (SELECT COUNT(*) FROM rule_suggestions WHERE status = 'pending')"
    ).fetchone()[0]
    alerts.send_telegram(
        "🟢 <b>bank-enrichment is fine</b> (weekly check-in)\n\n"
        f"{week} payment(s) this week · {answer} waiting for your answer · "
        f"{classify} waiting to be classified · {cards} card(s) waiting for a decision"
    )


def _after_run(code: int, today) -> None:
    if code == process.RAN and today.weekday() == 6:
        try:
            weekly_check_in()
        except Exception as e:
            log.error(f"Weekly check-in failed: {e}", exc_info=True)


def main(argv: list[str]) -> int:
    if "--now" in argv:
        return run_once()
    log.info(f"Worker started; runs daily at {RUN_AT:%H:%M} London time")
    while True:
        due = next_run(datetime.now(LONDON))
        log.info(f"Next run {due:%Y-%m-%d %H:%M %Z}")
        _sleep(max(0.0, (due - datetime.now(LONDON)).total_seconds()))
        _after_run(run_once(), datetime.now(LONDON))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
