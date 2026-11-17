"""The settings for the Telegram alerts: unusual payments, and subscriptions
(a changed price, a missed payment). Stored and read through app_settings, and set on
the Settings page's Alerts tab.
"""
from app_settings import Setting


# How far an unusual payment has to stand out, as the outlier cut-off (a modified
# z-score on log amounts). A lower cut-off flags more payments.
LEVELS = {"less": 4.5, "default": 3.5, "more": 2.5}
LEVEL_LABELS = {"less": "Less sensitive", "default": "Default", "more": "More sensitive"}

UNUSUAL_ON = Setting("unusual_payments_on", True)
UNUSUAL_LEVEL = Setting("unusual_payments_level", "default", choices=tuple(LEVELS))
UNUSUAL_MIN_HISTORY = Setting("unusual_payments_min_history", 5, 3, 24)
UNUSUAL_MIN_GAP = Setting("unusual_payments_min_gap", 5.0, 0, 100)

SUB_PRICE_ON = Setting("subscription_price_on", True)
SUB_PRICE_POUNDS = Setting("subscription_price_pounds", 0.50, 0, 50)
SUB_PRICE_PERCENT = Setting("subscription_price_percent", 5.0, 0, 50)
SUB_MISSED_ON = Setting("subscription_missed_on", True)
SUB_MISSED_GRACE = Setting("subscription_missed_grace_days", 7, 0, 60)

ALL = [UNUSUAL_ON, UNUSUAL_LEVEL, UNUSUAL_MIN_HISTORY, UNUSUAL_MIN_GAP,
       SUB_PRICE_ON, SUB_PRICE_POUNDS, SUB_PRICE_PERCENT, SUB_MISSED_ON, SUB_MISSED_GRACE]
