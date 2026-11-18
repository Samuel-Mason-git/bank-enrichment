"""/health: how the app is doing, sent to you on Telegram.

It checks what the server can see from where it runs: that it answers, that the
database is reachable, when the last Monzo payment arrived, how the last worker
run went, and whether the Claude key is set. It never calls Claude or Monzo, so
it costs nothing and can't use up a quota."""
import re
from datetime import datetime, timezone

import app_config
import run_schedule

HEALTH_COMMAND = re.compile(r"^/health(?:@\w+)?\s*$", re.IGNORECASE)

# The worker runs hourly from 08:00 to 22:00 London time, so a gap longer than
# this means the worker has stopped, not that it's the quiet hours.
WORKER_STALE_HOURS = 26


def is_health(text: str | None) -> bool:
    return bool(HEALTH_COMMAND.match((text or "").strip()))


def _ago(when: datetime | None) -> str:
    if when is None:
        return "never"
    seconds = int((datetime.now(timezone.utc) - when).total_seconds())
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


class Heartbeat:
    """Tells you once when the worker has missed a scheduled run, and once when it runs
    again. Remembers which missed run it reported, so a long gap alerts only once. A
    server restart starts it afresh, so at most one repeat after a restart."""

    def __init__(self):
        self.alerted_for: datetime | None = None

    def check(self, con, now: datetime) -> str | None:
        """The message to send now, if any."""
        last = con.execute("SELECT MAX(started_at) FROM runs").fetchone()[0]
        missed = run_schedule.missed_run(last, now)
        if missed is not None and self.alerted_for is None:
            self.alerted_for = missed
            since = "never" if last is None else f"{last.astimezone(run_schedule.LONDON):%a %d %b, %H:%M}"
            return ("⚠️ <b>The worker has stopped running</b>\n\n"
                    f"The {missed:%H:%M} run didn't start. Last run started: {since}.\n"
                    "Payments still arrive, but nothing is classified, and no summaries or budget messages go out "
                    "until it runs again. On the server: <code>docker compose ps</code>, then "
                    "<code>docker compose logs worker</code>.")
        if missed is None and self.alerted_for is not None:
            self.alerted_for = None
            return (f"✅ <b>The worker is running again</b>\n\nIts last run started "
                    f"{last.astimezone(run_schedule.LONDON):%H:%M}.")
        return None


def report(get_con) -> str:
    """The health message. get_con is server_db.get_con, so a database that's
    down is reported rather than raising."""
    lines = ["<b>Health</b>", "✅ Server: answering"]
    try:
        con = get_con()
        con.execute("SELECT 1").fetchone()
    except Exception:
        return "\n".join(lines + ["❌ Database: not reachable, so the rest can't be checked"])
    lines.append("✅ Database: reachable")

    last_payment = con.execute("SELECT MAX(received_at) FROM transactions_raw").fetchone()[0]
    lines.append(f"Last Monzo payment received: {_ago(last_payment)}")

    run = con.execute(
        "SELECT started_at, status FROM runs WHERE status <> 'skipped' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        lines.append("⚠️ Worker: no run recorded yet")
    else:
        started, status = run
        age_hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
        if age_hours > WORKER_STALE_HOURS:
            lines.append(f"⚠️ Worker: last run {_ago(started)}, so it may have stopped")
        elif status == "problems":
            lines.append(f"⚠️ Worker: last run {_ago(started)} had a step fail (see Activity on the dashboard)")
        elif status == "running":
            lines.append(f"Worker: a run is going, started {_ago(started)}")
        else:
            lines.append(f"✅ Worker: last run {_ago(started)}, all fine")

    if app_config.anthropic_key():
        lines.append("✅ Claude key: set")
    else:
        lines.append("❌ Claude key: not set (ANTHROPIC_API_KEY in config/.env)")
    return "\n".join(lines)
