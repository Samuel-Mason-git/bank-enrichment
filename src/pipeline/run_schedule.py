"""When the worker's runs should happen, and whether one has been missed.

The worker runs on the hour from FIRST_RUN to LAST_RUN, London time (worker.py).
A run is missed when the most recent of those hours, allowing GRACE for it to
start, has passed without a run starting. Overnight, the last expected run is
LAST_RUN the evening before, so the quiet hours never look like a missed run.

Used by the server's heartbeat (it alerts on Telegram), /health, and the Settings
page's setup check, so all three agree on what "stopped" means.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

FIRST_RUN, LAST_RUN = 8, 22      # hours, London time; the same as worker.py
LONDON = ZoneInfo("Europe/London")
GRACE = timedelta(minutes=30)    # how long a run may take to start before it counts as missed


def latest_expected_run(now: datetime) -> datetime:
    """The most recent scheduled run time that should have started by now (allowing GRACE).
    `now` is an aware datetime; the answer is in London time."""
    local = (now - GRACE).astimezone(LONDON)
    day = local.date()
    for _ in range(3):
        for hour in range(LAST_RUN, FIRST_RUN - 1, -1):
            candidate = datetime(day.year, day.month, day.day, hour, tzinfo=LONDON)
            if candidate <= local:
                return candidate
        day -= timedelta(days=1)
    raise AssertionError("unreachable: yesterday always has a run")


def missed_run(last_started: datetime | None, now: datetime) -> datetime | None:
    """The scheduled run that was missed, if the last run started before it; else None.
    A few minutes' leeway either side of the hour is allowed."""
    expected = latest_expected_run(now)
    if last_started is None or last_started < expected - timedelta(minutes=10):
        return expected
    return None
