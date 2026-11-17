"""Subscription alerts: a price that changed, and a payment that's gone missing."""
from datetime import timedelta
from unittest.mock import MagicMock

import database_functions as dbf
import subscription_alerts as sa
from tests.local_helpers import insert_transaction


def _london_now(db):
    return db.execute("SELECT NOW() AT TIME ZONE 'Europe/London'").fetchone()[0]


def _sub(db, name="StreamBox", amount=10.99, frequency="monthly", merchant="StreamBox"):
    return dbf.upsert_subscription(name, amount, frequency, merchant)


def _charge(db, txn_id, amount, days_ago, merchant="StreamBox"):
    insert_transaction(db, txn_id, amount=-amount, merchant_name=merchant,
                       created_at=_london_now(db) - timedelta(days=days_ago))


def test_a_price_rise_is_alerted_once(db, alert_post):
    _sub(db)
    _charge(db, "p1", 12.99, days_ago=0.1)
    assert sa.run() == 1
    assert sa.run() == 0                       # the same payment isn't alerted again
    assert alert_post.call_count == 1
    assert "up from £10.99" in alert_post.call_args.kwargs["json"]["text"]


def test_a_price_drop_is_alerted_as_down(db, alert_post):
    _sub(db, amount=10.99)
    _charge(db, "p1", 8.99, days_ago=0.1)
    assert sa.run() == 1
    assert "down from £10.99" in alert_post.call_args.kwargs["json"]["text"]


def test_the_same_price_or_a_penny_difference_is_not_alerted(db, alert_post):
    _sub(db, amount=10.99)
    _charge(db, "p1", 10.99, days_ago=0.1)
    assert sa.run() == 0
    assert alert_post.call_count == 0


def test_an_old_payment_is_not_a_price_alert(db, alert_post):
    _sub(db, amount=10.99)
    _charge(db, "p1", 12.99, days_ago=10)     # outside the lookback
    assert sa.run() == 0


def test_a_missed_monthly_payment_is_alerted_once_per_gap(db, alert_post):
    _sub(db, frequency="monthly")
    _charge(db, "last", 10.99, days_ago=50)   # 50 days: a month plus a week's grace, and then some
    assert sa.run() == 1
    assert sa.run() == 0
    assert "hasn't charged since" in alert_post.call_args.kwargs["json"]["text"]


def test_a_payment_within_the_grace_period_is_not_missed(db, alert_post):
    _sub(db, frequency="monthly")
    _charge(db, "last", 10.99, days_ago=35)   # 31 days plus 7 grace: 35 is still on time
    assert sa.run() == 0


def test_a_subscription_never_paid_is_not_checked(db, alert_post):
    _sub(db, merchant="Nothing Yet")
    assert sa.run() == 0


def test_an_inactive_subscription_is_not_checked(db, alert_post):
    sid = _sub(db)
    db.execute("UPDATE subscriptions SET active = FALSE WHERE id = %s", [sid])
    _charge(db, "p1", 12.99, days_ago=0.1)
    _charge(db, "last", 10.99, days_ago=50)
    assert sa.run() == 0


def test_a_failed_send_is_tried_again(db, alert_post):
    _sub(db)
    _charge(db, "p1", 12.99, days_ago=0.1)
    alert_post.return_value = MagicMock(status_code=500, ok=False)
    assert sa.run() == 0
    alert_post.return_value = MagicMock(status_code=200, ok=True)
    assert sa.run() == 1


def test_a_payment_after_a_gap_clears_it(db, alert_post):
    sid = _sub(db)
    _charge(db, "old", 10.99, days_ago=50)
    assert sa.run() == 1
    _charge(db, "newer", 10.99, days_ago=20)   # paid again: the gap is over
    assert sa.run() == 0
    assert db.execute("SELECT COUNT(*) FROM subscription_alerts WHERE sub_id = %s", [sid]).fetchone()[0] == 1


def test_an_old_gap_is_not_reported_on_the_first_run(db, alert_post):
    # a monthly subscription last paid 200 days ago: cancelled, or never deactivated
    dbf.upsert_subscription("OldPlan", 5.0, "monthly", "OldPlan")
    _charge(db, "old", 5.0, days_ago=200, merchant="OldPlan")
    assert sa.run() == 0
    assert alert_post.call_count == 0


def test_a_fresh_gap_is_reported(db, alert_post):
    # 50 days: past a month and a week's grace, and still within the two-week window
    dbf.upsert_subscription("NewGap", 5.0, "monthly", "NewGap")
    _charge(db, "fresh", 5.0, days_ago=50, merchant="NewGap")
    assert sa.run() == 1

