"""One run of the pipeline -- the worker on the server runs it every hour
through the day (worker.py), and it's safe to run by hand too.

The server stores every transaction as it arrives and records your answers,
so there's nothing to fetch: this works through what's waiting in the shared
database -- quick-taps, decisions made on Telegram, the classifier, the
monthly review, rule suggestions and the summaries. Any card it creates (a
category or taxonomy proposal, a rule suggestion) is sent by the server."""
import logging
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

import activity
import llm_calls
from database_functions import init_db, get_con, apply_quick_tap_classifications, log_path
from llm_labelling import run as run_classifier, regenerate_category_proposals, make_placement_reviewer
from monthly_summary import run as run_monthly_summary
from weekly_summary import run as run_weekly_summary
from taxonomy_review import run as run_taxonomy_review, collect_decisions as collect_taxonomy_decisions
from category_proposals import collect_decisions as collect_category_decisions
from rule_suggestions import sync_candidates as sync_rule_candidates

LOG_PATH = log_path("bank_enrichment.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=5),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# Only one run at a time, wherever it's started: two at once would classify
# the same transactions twice and send duplicate cards. A Postgres advisory
# lock, held by this run's connection and gone when it ends or drops.
RUN_LOCK = "bank_enrichment.process"

# What main() returns.
RAN, NO_DATABASE, ALREADY_RUNNING = 0, 1, 2


def _take_run_lock() -> bool:
    return get_con().execute("SELECT pg_try_advisory_lock(hashtext(%s))", [RUN_LOCK]).fetchone()[0]


def _release_run_lock() -> None:
    get_con().execute("SELECT pg_advisory_unlock(hashtext(%s))", [RUN_LOCK])


_db_log: activity.DatabaseLogHandler | None = None

# How long each step of the current run took, in seconds (runs.steps).
_step_seconds: dict[str, float] = {}


@contextmanager
def _timed(step: str):
    t0 = time.monotonic()
    try:
        yield
    finally:
        _step_seconds[step] = round(time.monotonic() - t0, 2)


def _quietly(what: str, fn, *args):
    """Activity bookkeeping: worth having, never worth failing a run over."""
    try:
        return fn(*args)
    except Exception as e:
        log.warning(f"Couldn't record the run's {what}: {e}")
        return None


def main() -> int:
    """One run: RAN, or NO_DATABASE / ALREADY_RUNNING if it couldn't start. A
    step that fails is logged and the rest still run. Each run, and every
    warning or error, is recorded for the dashboard's Activity page."""
    global _db_log
    run_start = time.time()
    log.info("--- Process run started ---")

    t0 = time.time()
    try:
        init_db()
        log.info(f"Database ready ({time.time() - t0:.2f}s)")
    except Exception as e:
        log.error(f"Couldn't connect to the database (is the VPN up?): {e}", exc_info=True)
        return NO_DATABASE
    if _db_log is None:
        _db_log = activity.attach("worker", get_con)

    if not _take_run_lock():
        log.warning("Another run is already in progress -- not starting a second one")
        _quietly("skip", activity.skipped_run, get_con())
        return ALREADY_RUNNING
    run_id = _quietly("start", activity.start_run, get_con())
    llm_calls.current_run_id = run_id
    _step_seconds.clear()
    crashed = False
    try:
        _steps()
    except BaseException:
        crashed = True
        raise
    finally:
        _release_run_lock()
        llm_calls.current_run_id = None
        if run_id is not None:
            summary = _quietly("summary", activity.run_summary, get_con(), run_id) or {}
            status = "problems" if crashed or summary.get("errors") else "ok"
            _quietly("end", activity.finish_run, get_con(), run_id, status, summary,
                     dict(_step_seconds), activity.memory())
            _quietly("clear-out", activity.clear_old, get_con())
    log.info(f"--- Run complete in {time.time() - run_start:.2f}s ---")
    return RAN


def _steps() -> None:
    with _timed("quick_taps"):
        try:
            t0 = time.time()
            # A tap is the user's own choice, but a mis-tap (a dentist filed under
            # Alcohol) is exactly what it can't catch -- so the placement judge
            # gets a look and can hold one behind a card instead.
            reviewer = make_placement_reviewer()
            quick_classified = apply_quick_tap_classifications(review=reviewer.review_tap)
            reviewer.sync()
            if quick_classified:
                log.info(f"Quick-tap classified {quick_classified} transactions without the LLM ({time.time() - t0:.2f}s)")
        except Exception as e:
            log.error(f"Quick-tap classification failed: {e}", exc_info=True)

    # Category-creation decisions first, so a transaction released by a denial
    # (or classified by an approval) is settled before this run's classifier
    # batches touch it again.
    with _timed("category_decisions"):
        try:
            applied = collect_category_decisions()
            if applied:
                log.info(f"Applied {applied} approved category proposal(s)")
        except Exception as e:
            log.error(f"Collecting category decisions failed: {e}", exc_info=True)

    # After decisions, before the classifier: a "Try again" tap leaves its
    # transactions held by the (now-denied) old proposal on purpose, so this
    # has to run before run_classifier() would otherwise leave them alone
    # (they're not in get_unclassified() while held).
    with _timed("regenerate_proposals"):
        try:
            regenerated = regenerate_category_proposals()
            if regenerated:
                log.info(f"Regenerated {regenerated} category proposal(s)")
        except Exception as e:
            log.error(f"Regenerating category proposals failed: {e}", exc_info=True)

    with _timed("classifier"):
        try:
            run_classifier()
        except Exception as e:
            log.error(f"LLM classification run failed: {e}", exc_info=True)

    # Decisions first, then this month's review -- so a proposal approved on the
    # phone is applied before anything new is proposed on top of it.
    with _timed("taxonomy_decisions"):
        try:
            applied = collect_taxonomy_decisions()
            if applied:
                log.info(f"Applied {applied} approved taxonomy proposal(s)")
        except Exception as e:
            log.error(f"Collecting taxonomy decisions failed: {e}", exc_info=True)

    with _timed("taxonomy_review"):
        try:
            proposed = run_taxonomy_review()
            if proposed:
                log.info(f"Proposed {len(proposed)} taxonomy change(s) for review")
        except Exception as e:
            log.error(f"Taxonomy review run failed: {e}", exc_info=True)

    with _timed("rule_suggestions"):
        try:
            suggested = sync_rule_candidates()
            if suggested:
                log.info(f"Suggested {suggested} rule(s)")
        except Exception as e:
            log.error(f"Rule suggestions failed: {e}", exc_info=True)

    with _timed("monthly_summary"):
        try:
            run_monthly_summary()
        except Exception as e:
            log.error(f"Monthly summary run failed: {e}", exc_info=True)

    with _timed("weekly_summary"):
        try:
            run_weekly_summary()
        except Exception as e:
            log.error(f"Weekly summary run failed: {e}", exc_info=True)


if __name__ == "__main__":
    raise SystemExit(main())
