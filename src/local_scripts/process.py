"""The pipeline's run (Task Scheduler runs it daily; safe to run by hand).

The server stores every transaction as it arrives and records your answers,
so there's nothing to fetch: this works through what's waiting in the shared
database -- quick-taps, decisions made on Telegram, the classifier, the
monthly review, rule suggestions and the summaries. Any card it creates (a
category or taxonomy proposal, a rule suggestion) is sent by the server."""
import logging
import time
from logging.handlers import RotatingFileHandler

from database_functions import init_db, apply_quick_tap_classifications, log_path
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


if __name__ == "__main__":
    run_start = time.time()
    log.info("--- Process run started ---")

    t0 = time.time()
    try:
        init_db()
        log.info(f"Database ready ({time.time() - t0:.2f}s)")
    except Exception as e:
        log.error(f"Couldn't connect to the database (is the VPN up?): {e}", exc_info=True)
        raise SystemExit(1)

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
    try:
        regenerated = regenerate_category_proposals()
        if regenerated:
            log.info(f"Regenerated {regenerated} category proposal(s)")
    except Exception as e:
        log.error(f"Regenerating category proposals failed: {e}", exc_info=True)

    try:
        run_classifier()
    except Exception as e:
        log.error(f"LLM classification run failed: {e}", exc_info=True)

    # Decisions first, then this month's review -- so a proposal approved on the
    # phone is applied before anything new is proposed on top of it.
    try:
        applied = collect_taxonomy_decisions()
        if applied:
            log.info(f"Applied {applied} approved taxonomy proposal(s)")
    except Exception as e:
        log.error(f"Collecting taxonomy decisions failed: {e}", exc_info=True)

    try:
        proposed = run_taxonomy_review()
        if proposed:
            log.info(f"Proposed {len(proposed)} taxonomy change(s) for review")
    except Exception as e:
        log.error(f"Taxonomy review run failed: {e}", exc_info=True)

    try:
        suggested = sync_rule_candidates()
        if suggested:
            log.info(f"Suggested {suggested} rule(s)")
    except Exception as e:
        log.error(f"Rule suggestions failed: {e}", exc_info=True)

    try:
        run_monthly_summary()
    except Exception as e:
        log.error(f"Monthly summary run failed: {e}", exc_info=True)

    try:
        run_weekly_summary()
    except Exception as e:
        log.error(f"Weekly summary run failed: {e}", exc_info=True)

    log.info(f"--- Run complete in {time.time() - run_start:.2f}s ---")
