"""The unusual-payment alert's settings: stored in app_settings, read by the
pipeline (unusual_payments.py) and set on the dashboard's Settings page.

The sensitivity levels are the outlier cut-off (a modified z-score on log
amounts) each one uses: a lower cut-off flags more payments.
"""
ON_SETTING = "unusual_payments_on"
LEVEL_SETTING = "unusual_payments_level"
DEFAULT_LEVEL = "default"
LEVELS = {"less": 4.5, "default": 3.5, "more": 2.5}
LEVEL_LABELS = {"less": "Less sensitive", "default": "Default", "more": "More sensitive"}
