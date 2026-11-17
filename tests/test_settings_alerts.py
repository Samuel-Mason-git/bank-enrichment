"""The Alerts tab on the Settings page saves the unusual-payment settings."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

from database_functions import get_setting

_DASHBOARD = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def test_the_alerts_tab_saves_off_and_more_sensitive(db):
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.switch_page("views/settings.py").run()
    assert not at.exception
    at.radio(key="unusual_level").set_value("more").run()
    at.toggle(key="unusual_on").set_value(False).run()
    at.button(key="unusual_save").click().run()
    assert not at.exception
    assert get_setting("unusual_payments_on") is False
    assert get_setting("unusual_payments_level") == "more"


def test_the_alerts_tab_starts_from_the_saved_settings(db):
    from database_functions import set_setting
    set_setting("unusual_payments_level", "less")
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.switch_page("views/settings.py").run()
    assert at.radio(key="unusual_level").value == "less"
    assert at.toggle(key="unusual_on").value is True
