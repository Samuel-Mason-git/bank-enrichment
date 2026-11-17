"""A Telegram alert for a payment far above what that merchant usually costs.

Checked on each hourly run, for payments from the last couple of days that
haven't been alerted on yet. A payment is unusual when the merchant has at
least MIN_HISTORY earlier payments in the past year, and this one is an
outlier against them: on a log scale, so a £3 coffee and a £300 shop are judged
by the same measure, and measured with the median and the median absolute
deviation, so one big past payment doesn't move the bar. A small gap in pounds
is never enough on its own. Each payment is
alerted on once; a send that fails is tried again on the next run.
"""
import html
import logging
import math
import statistics

from alerts import send_telegram
from database_functions import _rows, get_con, get_setting, now
import unusual_settings as cfg

log = logging.getLogger(__name__)

MIN_HISTORY = 5      # earlier payments to the merchant before comparing
LOOKBACK_DAYS = 2    # only payments this recent, so a first run doesn't alert on old history
HISTORY_DAYS = 365
OUTLIER_Z = cfg.LEVELS[cfg.DEFAULT_LEVEL]   # modified z-score on log amounts (Iglewicz and Hoaglin's cut-off)
MIN_SCALE = 0.15     # the spread is never taken as tighter than about ±15%, so a merchant that always costs the same doesn't alert on pence
MIN_GAP = 5.0        # and the payment is at least this many pounds above the usual


def unusual(spend: float, history: list[float], z: float = OUTLIER_Z) -> float | None:
    """The usual amount, when this spend is an outlier against that history; else None.

    Works on log amounts, so spends are judged by how far they are in
    proportion, not in pounds. The centre is the median and the spread is the
    median absolute deviation (scaled to match a standard deviation), with a floor
    so a merchant that always costs the same isn't judged on noise.
    """
    if len(history) < MIN_HISTORY or spend <= 0 or any(h <= 0 for h in history):
        return None
    logs = [math.log(h) for h in history]
    centre = statistics.median(logs)
    mad = statistics.median(abs(x - centre) for x in logs)
    scale = max(1.4826 * mad, MIN_SCALE)
    score = (math.log(spend) - centre) / scale
    usual = math.exp(centre)
    if score < z or spend - usual < MIN_GAP:
        return None
    return usual


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
    """Send the alerts that are due. Returns how many were sent. Off, it sends nothing."""
    if not get_setting(cfg.ON_SETTING, True):
        return 0
    level = get_setting(cfg.LEVEL_SETTING, cfg.DEFAULT_LEVEL)
    z = cfg.LEVELS.get(level, cfg.LEVELS[cfg.DEFAULT_LEVEL])
    sent = 0
    for txn in _recent_unalerted():
        merchant = txn["merchant_name"] or txn["counterparty_name"]
        if not merchant:
            continue
        spend = -float(txn["amount"])
        history = _history(merchant, txn["id"])
        usual = unusual(spend, history, z)
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
