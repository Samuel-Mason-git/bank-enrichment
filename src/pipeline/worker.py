"""Runs the pipeline on the server (the `worker` container in
docker-compose.yml) -- so classification, proposals and summaries happen even
when the PC is off.

Every hour on the hour from FIRST_RUN to LAST_RUN, London time, it runs
process.py's steps; nothing runs overnight, so no card ever arrives at 3am. A
run with nothing new to do makes no Claude calls. The weekly and monthly
summaries, and the monthly review, go out in the first run after their period
ends -- the morning's.

A run that can't start or crashes is logged, and Telegram hears about it once
when runs start failing and once when they work again -- not every hour in
between. Each kind of alert (out of credit, say) is sent at most once a day.
On Sundays it also says it's fine, after the day's first good run, so a worker
that has quietly stopped doesn't go unnoticed for long.

`python worker.py --now` runs once straight away and exits, for trying it by
hand."""
import logging
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import alerts
import process

FIRST_RUN, LAST_RUN = 8, 22   # hours, London time; runs on the hour, both included
LONDON = ZoneInfo("Europe/London")

log = logging.getLogger("worker")

# The wait until the next run -- a name of its own, so tests can skip it.
_sleep = time.sleep

# What the worker remembers between runs.
_failing_since: datetime | None = None   # set while runs aren't working
_alerts_day: date | None = None          # the day alerts were last reset for
_checked_in: date | None = None          # the last Sunday it said it's fine


def next_run(now: datetime) -> datetime:
    """The next whole hour from FIRST_RUN to LAST_RUN on London's clock,
    strictly after `now` (an aware datetime). Worked out on the wall clock, so
    it stays on the hour across the clocks going forward or back."""
    local = now.astimezone(LONDON)
    for day in (local.date(), local.date() + timedelta(days=1)):
        for hour in range(FIRST_RUN, LAST_RUN + 1):
            candidate = datetime(day.year, day.month, day.day, hour, tzinfo=LONDON)
            if candidate > local:
                return candidate
    raise AssertionError("unreachable: tomorrow always has a run")


def _reset_alerts_daily(today: date) -> None:
    global _alerts_day
    if _alerts_day != today:
        alerts.start_run()
        _alerts_day = today


def run_once(now: datetime | None = None) -> int:
    """One run. Returns process.main()'s answer, or 1 if the run crashed."""
    global _failing_since
    now = (now or datetime.now(LONDON)).astimezone(LONDON)
    _reset_alerts_daily(now.date())
    try:
        code = process.main()
        problem = {process.NO_DATABASE: "it couldn't reach the database",
                   process.ALREADY_RUNNING: "another run was already going (one started by hand?)"}.get(code)
    except Exception as e:  # the steps catch their own; this is the run itself failing
        log.error(f"The run crashed: {e}", exc_info=True)
        code, problem = 1, f"it crashed ({type(e).__name__})"

    if problem and code != process.ALREADY_RUNNING:
        if _failing_since is None:
            _failing_since = now
            alerts.send_telegram(f"⚠️ <b>The pipeline's runs have stopped working</b>\n\n"
                                 f"The {now:%H:%M} run didn't happen: {problem}. It tries again every hour, "
                                 f"and says so once it works.")
    elif code == process.ALREADY_RUNNING:
        log.warning("Another run was already going -- skipped this one")
    elif _failing_since is not None:
        alerts.send_telegram(f"✅ <b>The pipeline is running again</b>\n\n"
                             f"Runs had failed since {_failing_since:%a %H:%M}; the {now:%H:%M} one worked.")
        _failing_since = None
    return code


def weekly_check_in() -> None:
    """Sunday's "all fine", with what's waiting on you."""
    from database_functions import get_con
    con = get_con()
    week = con.execute("SELECT COUNT(*) FROM transactions_raw WHERE received_at >= now() - INTERVAL '7 days'").fetchone()[0]
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


def _after_run(code: int, today: date) -> None:
    """Sunday's check-in, once, after the day's first good run."""
    global _checked_in
    if code == process.RAN and today.weekday() == 6 and _checked_in != today:
        _checked_in = today
        try:
            weekly_check_in()
        except Exception as e:
            log.error(f"Weekly check-in failed: {e}", exc_info=True)


def main(argv: list[str]) -> int:
    if "--now" in argv:
        return run_once()
    log.info(f"Worker started; runs hourly from {FIRST_RUN:02d}:00 to {LAST_RUN:02d}:00 London time")
    while True:
        due = next_run(datetime.now(LONDON))
        log.info(f"Next run {due:%Y-%m-%d %H:%M %Z}")
        _sleep(max(0.0, (due - datetime.now(LONDON)).total_seconds()))
        now = datetime.now(LONDON)
        _after_run(run_once(now), now.date())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
