"""The alert settings: defaults, what's accepted, and what the checks do with them."""
from datetime import timedelta

import pytest
import app_settings

import alert_settings as AS
import subscription_alerts as sa
import unusual_payments as up
from database_functions import set_setting
from tests.local_helpers import insert_transaction


def test_an_unset_setting_gives_its_default(db):
    assert app_settings.get(AS.UNUSUAL_MIN_HISTORY) == 5
    assert app_settings.get(AS.SUB_MISSED_GRACE) == 7


def test_a_stored_value_out_of_range_falls_back_to_the_default(db):
    set_setting(AS.UNUSUAL_MIN_HISTORY.key, 500)
    assert app_settings.get(AS.UNUSUAL_MIN_HISTORY) == 5


def test_a_stored_value_of_the_wrong_type_falls_back_to_the_default(db):
    set_setting(AS.SUB_PRICE_ON.key, "yes")
    assert app_settings.get(AS.SUB_PRICE_ON) is True


def test_save_refuses_an_out_of_range_value(db):
    with pytest.raises(ValueError):
        app_settings.save(AS.UNUSUAL_MIN_HISTORY, 2)
    with pytest.raises(ValueError):
        app_settings.save(AS.UNUSUAL_LEVEL, "extreme")


def test_save_then_get_round_trips(db):
    app_settings.save(AS.SUB_PRICE_PERCENT, 12.5)
    app_settings.save(AS.UNUSUAL_MIN_HISTORY, 8.0)           # a float that's a whole number is kept as a count
    assert app_settings.get(AS.SUB_PRICE_PERCENT) == 12.5
    assert app_settings.get(AS.UNUSUAL_MIN_HISTORY) == 8


def test_a_higher_minimum_history_stops_an_alert(db, alert_post):
    _history(db, "Corner Pharmacy", [20, 22, 18, 21, 19])
    _payment(db, 80)
    app_settings.save(AS.UNUSUAL_MIN_HISTORY, 8)
    assert up.run() == 0


def test_a_larger_minimum_gap_stops_an_alert(db, alert_post):
    _history(db, "Corner Pharmacy", [20, 22, 18, 21, 19])
    _payment(db, 80)                               # £60 above the usual
    app_settings.save(AS.UNUSUAL_MIN_GAP, 100)
    assert up.run() == 0


def test_switching_unusual_alerts_off_stops_them(db, alert_post):
    _history(db, "Corner Pharmacy", [20, 22, 18, 21, 19])
    _payment(db, 80)
    app_settings.save(AS.UNUSUAL_ON, False)
    assert up.run() == 0


def test_a_bigger_price_change_threshold_stops_a_price_alert(db, alert_post):
    _subscription(db, amount=10.99)
    _charge(db, "p1", 12.99, days_ago=0.1)         # £2 up
    app_settings.save(AS.SUB_PRICE_POUNDS, 5)
    assert sa.run() == 0


def test_switching_price_alerts_off_stops_them(db, alert_post):
    _subscription(db, amount=10.99)
    _charge(db, "p1", 12.99, days_ago=0.1)
    app_settings.save(AS.SUB_PRICE_ON, False)
    assert sa.run() == 0


def test_a_longer_grace_stops_a_missed_alert(db, alert_post):
    _subscription(db)
    _charge(db, "last", 10.99, days_ago=50)        # 31 days plus 7 is 38; 50 is late with the default
    app_settings.save(AS.SUB_MISSED_GRACE, 30)               # 31 plus 30 is 61, so 50 isn't late any more
    assert sa.run() == 0


def test_switching_missed_alerts_off_stops_them(db, alert_post):
    _subscription(db)
    _charge(db, "last", 10.99, days_ago=50)
    app_settings.save(AS.SUB_MISSED_ON, False)
    assert sa.run() == 0


def _history(db, merchant, spends, days_ago=30):
    now = db.execute("SELECT NOW() AT TIME ZONE 'Europe/London'").fetchone()[0]
    for i, spend in enumerate(spends):
        insert_transaction(db, f"h{i}", amount=-spend, merchant_name=merchant,
                           created_at=now - timedelta(days=days_ago + i))


def _payment(db, amount, merchant="Corner Pharmacy", txn_id="new"):
    now = db.execute("SELECT NOW() AT TIME ZONE 'Europe/London'").fetchone()[0]
    insert_transaction(db, txn_id, amount=-amount, merchant_name=merchant, created_at=now - timedelta(hours=1))


def _subscription(db, amount=10.99):
    import database_functions as dbf
    return dbf.upsert_subscription("StreamBox", amount, "monthly", "StreamBox")


def _charge(db, txn_id, amount, days_ago, merchant="StreamBox"):
    now = db.execute("SELECT NOW() AT TIME ZONE 'Europe/London'").fetchone()[0]
    insert_transaction(db, txn_id, amount=-amount, merchant_name=merchant,
                       created_at=now - timedelta(days=days_ago))
