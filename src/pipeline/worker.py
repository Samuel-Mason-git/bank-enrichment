"""Runs the pipeline every morning, on the server (the `worker` container in
docker-compose.yml) -- so classification, proposals and summaries happen even
when the PC is off.

Sleeps until the next RUN_AT London time, runs process.py's steps, and
repeats. A run that can't start, or crashes, is logged and sent to Telegram,
and the worker carries on to the next morning. `python worker.py --now` runs
once straight away and exits, for trying it by hand."""
import logging
import sys
import time
from datetime import datetime, timedelta, time as clock
from zoneinfo import ZoneInfo

import alerts
import process

RUN_AT = clock(8, 0)
LONDON = ZoneInfo("Europe/London")

log = logging.getLogger("worker")


def next_run(now: datetime) -> datetime:
    """The next RUN_AT on London's clock, strictly after `now` (an aware
    datetime). Worked out on the wall clock, so it stays at 08:00 across the
    clocks going forward or back."""
    local = now.astimezone(LONDON)
    candidate = datetime.combine(local.date(), RUN_AT, tzinfo=LONDON)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), RUN_AT, tzinfo=LONDON)
    return candidate


def run_once() -> int:
    alerts.start_run()
    try:
        code = process.main()
    except Exception as e:  # the steps catch their own; this is the run itself failing
        log.error(f"The daily run crashed: {e}", exc_info=True)
        alerts.send_alert("worker-crash", "The daily run crashed",
                          f"{type(e).__name__}. The worker carries on and tries again tomorrow at 08:00.")
        return 1
    if code:
        alerts.send_alert("worker-not-started", "The daily run didn't start",
                          "It couldn't reach the database, or another run was already going. "
                          "It tries again tomorrow at 08:00.")
    return code


def main(argv: list[str]) -> int:
    if "--now" in argv:
        return run_once()
    log.info(f"Worker started; runs daily at {RUN_AT:%H:%M} London time")
    while True:
        due = next_run(datetime.now(LONDON))
        log.info(f"Next run {due:%Y-%m-%d %H:%M %Z}")
        time.sleep(max(0.0, (due - datetime.now(LONDON)).total_seconds()))
        run_once()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
