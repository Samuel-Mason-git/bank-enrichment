"""A Telegram alert for a payment far above what that merchant usually costs.

Checked on each hourly run, for payments from the last couple of days that
haven't been alerted on yet. A payment is unusual when the merchant has at
least MIN_HISTORY earlier payments in the past year, and this one is both
MULTIPLE times their median and at least MARGIN pounds more. Each payment is
alerted on once; a send that fails is tried again on the next run.
"""
import html
import logging
import statistics

from alerts import send_telegram
from database_functions import _rows, get_con, now

log = logging.getLogger(__name__)

MIN_HISTORY = 5      # earlier payments to the merchant before comparing
LOOKBACK_DAYS = 2    # only payments this recent, so a first run doesn't alert on old history
HISTORY_DAYS = 365
MULTIPLE = 2.0       # at least twice the usual amount ...
MARGIN = 20.0        # ... and at least this many pounds more


def unusual(spend: float, history: list[float]) -> float | None:
    """The usual amount, when this spend is unusual against that history; else None."""
    if len(history) < MIN_HISTORY:
        return None
    usual = statistics.median(history)
    return usual if spend >= max(MULTIPLE * usual, usual + MARGIN) else None


def alert_text(merchant: str, spend: float, usual: float, count: int) -> str:
    return (f"💸 <b>Unusual payment</b>\n"
            f"£{spend:,.2f} to {html.escape(merchant, quote=False)}. You usually pay about "
            f"£{usual:,.2f} (from {count} payments in the past year).")


def _recent_unalerted() -> list[dict]:
    return _rows(
        """SELECT t.id, t.amount, t.merchant_name, t.counterparty_name
           FROM transactions t
           WHERE t.amount < 0 AND NOT t.skipped
             AND t.created_at >= (NOW() AT TIME ZONE 'Europe/London') - make_interval(days => %s)
             AND NOT EXISTS (SELECT 1 FROM unusual_payment_alerts u WHERE u.txn_id = t.id)
           ORDER BY t.created_at""",
        [LOOKBACK_DAYS],
    )


def _history(merchant: str, exclude_id: str) -> list[float]:
    rows = _rows(
        """SELECT -t.amount AS spend
           FROM transactions t
           WHERE t.amount < 0 AND NOT t.skipped AND t.id <> %s
             AND COALESCE(t.merchant_name, t.counterparty_name) = %s
             AND t.created_at >= (NOW() AT TIME ZONE 'Europe/London') - make_interval(days => %s)""",
        [exclude_id, merchant, HISTORY_DAYS],
    )
    return [float(r["spend"]) for r in rows]


def run() -> int:
    """Send the alerts that are due. Returns how many were sent."""
    sent = 0
    for txn in _recent_unalerted():
        merchant = txn["merchant_name"] or txn["counterparty_name"]
        if not merchant:
            continue
        spend = -float(txn["amount"])
        history = _history(merchant, txn["id"])
        usual = unusual(spend, history)
        if usual is None:
            continue
        if not send_telegram(alert_text(merchant, spend, usual, len(history))):
            log.warning("Unusual-payment alert not sent: Telegram refused it; it will be tried again")
            continue
        get_con().execute(
            "INSERT INTO unusual_payment_alerts (txn_id, sent_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [txn["id"], now()],
        )
        sent += 1
        log.info("Sent an unusual-payment alert")
    return sent
