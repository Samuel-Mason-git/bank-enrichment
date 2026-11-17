"""Telegram alerts about your subscriptions, from the payments that come in.

Two kinds, each sent once:
  - price: the newest payment to a subscription's merchant is a different
    amount from the one stored for it, by at least 50p and 5%. Keyed by that
    payment, so a later payment at the new price doesn't alert again.
  - missed: the last payment to the merchant is older than the subscription's
    period plus a grace period. Keyed by that last payment, so the same gap
    alerts once, and a new gap later alerts again.

Subscriptions are detected on the dashboard, which uses pandas. The worker
doesn't carry pandas, so these checks are plain SQL over the stored
subscriptions and the transactions. A subscription with no payment ever seen
isn't checked.
"""
import html
import logging
from datetime import date

from alerts import send_telegram
import alert_settings as AS
from database_functions import _rows, get_con, now

log = logging.getLogger(__name__)

PRICE_LOOKBACK_DAYS = 3   # only a payment this recent can show a new price, so a first run doesn't alert on old ones
PERIOD_DAYS = {"weekly": 7, "fortnightly": 14, "monthly": 31, "annual": 366}

_MATCH = "COALESCE(t.merchant_name, t.counterparty_name) = COALESCE(s.merchant_name, s.name)"


def price_changed(stored: float, paid: float, pounds: float, percent: float) -> bool:
    """Whether a payment differs from the stored amount enough to mention: at least
    `pounds`, and at least `percent` of the stored amount."""
    return abs(paid - stored) >= max(pounds, percent / 100 * stored)


def missed(days_since: int, frequency: str, grace_days: int) -> bool:
    return days_since > PERIOD_DAYS[frequency] + grace_days


def _london_now():
    return get_con().execute("SELECT NOW() AT TIME ZONE 'Europe/London'").fetchone()[0]


def _sent(sub_id: int, kind: str, ref: str) -> bool:
    return bool(get_con().execute(
        "SELECT 1 FROM subscription_alerts WHERE sub_id = %s AND kind = %s AND ref = %s",
        [sub_id, kind, ref]).fetchone())


def _record(sub_id: int, kind: str, ref: str) -> None:
    get_con().execute(
        "INSERT INTO subscription_alerts (sub_id, kind, ref, sent_at) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        [sub_id, kind, ref, now()])


def _price_alert(name: str, stored: float, paid: float) -> str:
    direction = "up from" if paid > stored else "down from"
    return (f"🔁 <b>{html.escape(name, quote=False)}</b> charged £{paid:,.2f}, {direction} £{stored:,.2f} "
            f"on your subscriptions. If that's the new price, update it on the Subscriptions page.")


def _missed_alert(name: str, last: date, frequency: str) -> str:
    return (f"⏳ <b>{html.escape(name, quote=False)}</b> hasn't charged since {last.day} {last:%b}. "
            f"It's usually {frequency}. Cancelled, or just late?")


def _price_candidates() -> list[dict]:
    return _rows(
        f"""SELECT s.id, s.name, s.amount, t.id AS txn_id, -t.amount AS paid
            FROM subscriptions s
            JOIN LATERAL (
                SELECT t.id, t.amount, t.created_at
                FROM transactions t
                WHERE t.amount < 0 AND NOT t.skipped AND {_MATCH}
                ORDER BY t.created_at DESC LIMIT 1
            ) t ON TRUE
            WHERE s.active
              AND t.created_at >= (NOW() AT TIME ZONE 'Europe/London') - make_interval(days => %s)""",
        [PRICE_LOOKBACK_DAYS],
    )


def _last_seen() -> list[dict]:
    return _rows(
        f"""SELECT s.id, s.name, s.frequency, MAX(t.created_at) AS last_seen
            FROM subscriptions s
            JOIN transactions t ON t.amount < 0 AND NOT t.skipped AND {_MATCH}
            WHERE s.active
            GROUP BY s.id, s.name, s.frequency"""
    )


def run() -> int:
    """Send the alerts that are due. Returns how many were sent. Each kind is
    checked only while it's switched on."""
    sent = 0
    price_on = AS.get(AS.SUB_PRICE_ON)
    pounds = float(AS.get(AS.SUB_PRICE_POUNDS))
    percent = float(AS.get(AS.SUB_PRICE_PERCENT))
    for row in _price_candidates() if price_on else []:
        stored, paid = float(row["amount"]), float(row["paid"])
        if not price_changed(stored, paid, pounds, percent) or _sent(row["id"], "price", row["txn_id"]):
            continue
        if send_telegram(_price_alert(row["name"], stored, paid)):
            _record(row["id"], "price", row["txn_id"])
            sent += 1
            log.info("Sent a subscription price alert")
        else:
            log.warning("Subscription price alert not sent: Telegram refused it; it will be tried again")

    if not AS.get(AS.SUB_MISSED_ON):
        return sent
    grace = AS.get(AS.SUB_MISSED_GRACE)
    today = _london_now()
    for row in _last_seen():
        last = row["last_seen"]
        days_since = (today - last).days
        if not missed(days_since, row["frequency"], grace):
            continue
        ref = last.date().isoformat()
        if _sent(row["id"], "missed", ref):
            continue
        if send_telegram(_missed_alert(row["name"], last.date(), row["frequency"])):
            _record(row["id"], "missed", ref)
            sent += 1
            log.info("Sent a missed-subscription alert")
        else:
            log.warning("Missed-subscription alert not sent: Telegram refused it; it will be tried again")
    return sent
