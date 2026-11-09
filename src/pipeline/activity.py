"""What the app itself did, kept in the database so the dashboard's Activity
page can show any day: each pipeline run (when, how long, what it did) and every
warning or error the server or the worker logged.

Both sides use it: the worker through process.py, the server through main.py
(which puts src/pipeline on its path for this). Nothing here may stop the app
working -- if the database can't take a log line, the line is still in the
container's own log and the app carries on."""
import json
import logging
import threading
import traceback
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_DAYS = 90


class DatabaseLogHandler(logging.Handler):
    """Copies WARNING and above into log_events, through the process's own
    database connection (get_con, the caller's usual one -- autocommit, so
    each line is saved as it's written)."""

    def __init__(self, service: str, get_con, level=logging.WARNING):
        super().__init__(level)
        self.service = service
        self._get_con = get_con
        self._busy = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._busy, "on", False):  # a log line from inside this very write
            return
        self._busy.on = True
        try:
            detail = "".join(traceback.format_exception(*record.exc_info))[-8000:] if record.exc_info else None
            self._get_con().execute(
                "INSERT INTO log_events (service, level, logger, message, detail) VALUES (%s, %s, %s, %s, %s)",
                [self.service, record.levelname, record.name, record.getMessage()[:4000], detail],
            )
        except Exception:
            pass  # never let logging break the app, nor log about failing to log
        finally:
            self._busy.on = False


def attach(service: str, get_con) -> DatabaseLogHandler:
    """Send this process's warnings and errors to log_events too. Once per
    process: a second call (the server starting again in tests) reuses it."""
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, DatabaseLogHandler) and existing.service == service:
            existing._get_con = get_con
            return existing
    handler = DatabaseLogHandler(service, get_con)
    logging.getLogger().addHandler(handler)
    return handler


# ── Runs (process.py) ─────────────────────────────────────────────────────────

def start_run(con) -> int:
    return con.execute(
        "INSERT INTO runs (started_at, status) VALUES (now(), 'running') RETURNING run_id"
    ).fetchone()[0]


def finish_run(con, run_id: int, status: str, summary: dict, steps: dict | None = None,
               memory: dict | None = None) -> None:
    con.execute(
        "UPDATE runs SET finished_at = now(), status = %s, summary = %s, steps = %s, memory = %s WHERE run_id = %s",
        [status, json.dumps(summary), json.dumps(steps or {}), json.dumps(memory or {}), run_id],
    )


# The container's memory, from its cgroup (cgroup v2), as the pipelines
# project reads it. memory.peak is since the container started (the worker
# is long-lived), so memory.current at the end of a run is the per-run figure.
CGROUP = Path("/sys/fs/cgroup")


def memory() -> dict:
    """{"current_mb", "peak_mb", "limit_mb"} as far as they can be read --
    empty off Linux or without the files."""
    found = {}
    for key, name in (("current_mb", "memory.current"), ("peak_mb", "memory.peak"), ("limit_mb", "memory.max")):
        try:
            value = (CGROUP / name).read_text().strip()
            if value != "max":
                found[key] = round(int(value) / 1_048_576)
        except (OSError, ValueError):
            pass
    return found


def skipped_run(con) -> None:
    con.execute(
        "INSERT INTO runs (started_at, finished_at, status) VALUES (now(), now(), 'skipped')"
    )


def run_summary(con, run_id: int) -> dict:
    """What a run did, read off the history it wrote while it ran: labels by
    who set them, cards it created, and how many errors were logged."""
    started = con.execute("SELECT started_at FROM runs WHERE run_id = %s", [run_id]).fetchone()[0]
    summary = {}
    for source, n in con.execute(
        "SELECT source, COUNT(*) FROM label_events WHERE set_at >= %s AND parent_id IS NOT NULL GROUP BY source",
        [started],
    ).fetchall():
        summary[f"labelled_{source}"] = n
    for name, table in (("category_cards", "category_proposals"), ("taxonomy_cards", "taxonomy_proposals"),
                        ("rule_suggestions", "rule_suggestions")):
        n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE proposed_at >= %s", [started]).fetchone()[0]
        if n:
            summary[name] = n
    calls, spent = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE run_id = %s", [run_id]
    ).fetchone()
    if calls:
        summary["llm_calls"] = calls
        summary["llm_cost_usd"] = round(float(spent), 4)
    errors = con.execute(
        "SELECT COUNT(*) FROM log_events WHERE service = 'worker' AND level IN ('ERROR', 'CRITICAL') "
        "AND logged_at >= %s", [started],
    ).fetchone()[0]
    if errors:
        summary["errors"] = errors
    return summary


def clear_old(con) -> None:
    con.execute(f"DELETE FROM log_events WHERE logged_at < now() - INTERVAL '{KEEP_DAYS} days'")
    con.execute(f"DELETE FROM runs WHERE started_at < now() - INTERVAL '{KEEP_DAYS} days'")
