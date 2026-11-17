"""Settings for the Ask agent: the dashboard's Ask page and the Telegram /ask
command. Stored in app_settings and set on the Settings page's Ask tab, with
the same defaults, ranges and fallbacks as the alert settings (alert_settings).
"""
from alert_settings import Setting, get, save  # noqa: F401  (re-exported for the callers)

MODELS = {
    "claude-haiku-5-5": "Haiku 5.5 (default, cheapest)",
    "claude-sonnet-5-5": "Sonnet 5.5 (more capable, several times the cost)",
}
EFFORTS = {"low": "Low", "medium": "Medium", "high": "High (slowest, most thorough)"}

PAGE_ON = Setting("ask_page_on", True)
TELEGRAM_ON = Setting("ask_telegram_on", True)
MODEL = Setting("ask_model", "claude-haiku-5-5", choices=tuple(MODELS))
EFFORT = Setting("ask_effort", "medium", choices=tuple(EFFORTS))
CHAT_MINUTES = Setting("ask_chat_minutes", 30, 5, 240)
PROPOSAL_MINUTES = Setting("ask_proposal_minutes", 30, 5, 240)

ALL = [PAGE_ON, TELEGRAM_ON, MODEL, EFFORT, CHAT_MINUTES, PROPOSAL_MINUTES]
