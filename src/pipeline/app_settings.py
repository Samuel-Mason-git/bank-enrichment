"""The shared settings machinery: a Setting (its key, default and range), and get and
save for it. Stored in app_settings, read by the pipeline and set on the Settings
page. A stored value that's missing or out of range falls back to the default, and
save() refuses a value out of range. Each feature's settings live in its own module
(alert_settings, ask_settings, classifier_settings) and use these.
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
