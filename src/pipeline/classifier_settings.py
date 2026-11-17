"""The model the classifier's passes 1 to 3 use: the parent category, the
subcategory, and the alternatives. Stored in app_settings and set on the
Settings page's Classification tab. The match against existing categories and
the placement judge stay on Haiku (see llm_labelling.JOBS).
"""
from alert_settings import Setting, get, save  # noqa: F401  (the callers use get and save from here)

MODELS = {
    "claude-sonnet-4-6": "Sonnet 4.6 (the default)",
    "claude-sonnet-5-5": "Sonnet 5.5 (newer; check it on your own history first)",
    "claude-haiku-5-5": "Haiku 5.5 (cheapest; check it on your own history first)",
}
MODEL = Setting("classifier_model", "claude-sonnet-4-6", choices=tuple(MODELS))
