"""The settings for the Telegram alerts: unusual payments, and subscriptions
(a changed price, a missed payment).

Stored in app_settings, read by the pipeline's hourly checks and set on the
Settings page's Alerts tab. Each setting has a default and a range. A stored
value that's missing or out of range falls back to the default, and save()
refuses a value out of range.
"""
from dataclasses import dataclass

from database_functions import get_setting, set_setting


@dataclass(frozen=True)
class Setting:
    key: str
    default: object
    low: float | None = None
    high: float | None = None
    choices: tuple | None = None


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


def _valid(setting: Setting, value) -> bool:
    if isinstance(setting.default, bool):
        return isinstance(value, bool)
    if setting.choices is not None:
        return value in setting.choices
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return setting.low is None or setting.low <= value <= setting.high


def get(setting: Setting):
    """The saved value, or the default when it's unset, or out of range."""
    try:
        value = get_setting(setting.key, setting.default)
    except Exception:
        return setting.default
    if isinstance(setting.default, int) and not isinstance(setting.default, bool) and isinstance(value, float):
        value = int(value)
    return value if _valid(setting, value) else setting.default


def save(setting: Setting, value) -> None:
    """Save a value. Raises ValueError for one outside the setting's range."""
    if isinstance(setting.default, int) and not isinstance(setting.default, bool) and isinstance(value, float):
        value = int(value)
    if not _valid(setting, value):
        raise ValueError(f"{setting.key} must be {setting.choices or f'between {setting.low} and {setting.high}'}")
    set_setting(setting.key, value)
