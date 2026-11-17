"""The unusual-payment alert: a payment far above what its merchant usually costs."""
from datetime import timedelta
from unittest.mock import MagicMock

import unusual_payments as up
from tests.local_helpers import insert_transaction


def _london_now(db):
    return db.execute("SELECT NOW() AT TIME ZONE 'Europe/London'").fetchone()[0]


def _history(db, merchant, spends, days_ago=30):
    now = _london_now(db)
    for i, spend in enumerate(spends):
        insert_transaction(db, f"h{i}", amount=-spend, merchant_name=merchant,
                           created_at=now - timedelta(days=days_ago + i))


def _payment(db, amount, merchant="Corner Pharmacy", hours_ago=1, skipped=False, txn_id="new"):
    insert_transaction(db, txn_id, amount=-amount, merchant_name=merchant,
                       created_at=_london_now(db) - timedelta(hours=hours_ago), skipped=skipped)


def test_far_above_the_usual_is_alerted_once(db, alert_post):
    _history(db, "Corner Pharmacy", [20, 22, 18, 21, 19])
    _payment(db, 80)
    assert up.run() == 1
    assert up.run() == 0                      # already alerted: not again
    assert alert_post.call_count == 1
    assert "Unusual payment" in alert_post.call_args.kwargs["json"]["text"]


def test_close_to_the_usual_is_not_alerted(db, alert_post):
    _history(db, "Corner Pharmacy", [20, 22, 18, 21, 19])
    _payment(db, 30)                          # about 1.5 times the usual: within normal variation
    assert up.run() == 0
    assert alert_post.call_count == 0


def test_too_little_history_is_not_alerted(db, alert_post):
    _history(db, "Corner Pharmacy", [20, 22, 18, 21])   # four earlier payments, not five
    _payment(db, 200)
    assert up.run() == 0


def test_old_payments_are_not_checked(db, alert_post):
    _history(db, "Corner Pharmacy", [20, 22, 18, 21, 19])
    _payment(db, 200, hours_ago=24 * 5)       # five days ago: outside the lookback
    assert up.run() == 0


def test_skipped_payments_are_not_alerted(db, alert_post):
    _history(db, "Corner Pharmacy", [20, 22, 18, 21, 19])
    _payment(db, 200, skipped=True)
    assert up.run() == 0


def test_a_failed_send_is_tried_again(db, alert_post):
    _history(db, "Corner Pharmacy", [20, 22, 18, 21, 19])
    _payment(db, 80)
    alert_post.return_value = MagicMock(status_code=500, ok=False)
    assert up.run() == 0
    alert_post.return_value = MagicMock(status_code=200, ok=True)
    assert up.run() == 1


def test_the_usual_is_the_median_not_the_mean():
    # one big past payment doesn't move the bar: the median is what counts
    assert up.unusual(80, [20, 20, 20, 20, 500]) is not None
    assert up.unusual(30, [20, 20, 20, 20, 500]) is None


def test_a_small_merchant_is_judged_in_proportion_not_in_pounds():
    # a £3 coffee that usually costs £3: £10 is more than three times that, so it's an outlier
    coffee = [3, 2.8, 3.2, 3, 2.9]
    assert up.unusual(10, coffee) is not None
    # a £150 shop usually costs £150: £200 is only about a third more, so it isn't
    shop = [140, 150, 160, 145, 155]
    assert up.unusual(200, shop) is None
    assert up.unusual(400, shop) is not None


def test_the_pound_gap_stops_pence_alerts():
    # £2 against £1: four times the usual, but only £1 more, so not an alert
    assert up.unusual(2, [1, 1.05, 0.95, 1, 1]) is None
