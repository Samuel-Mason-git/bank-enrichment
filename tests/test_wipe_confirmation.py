"""The danger zone's wipe: the confirmation is typed, the button runs, and the page
doesn't raise when it does (Streamlit refuses a widget's state changed after it's shown)."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

_DASHBOARD = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def _wipe_button(at):
    return [b for b in at.button if b.label == "Permanently wipe entire taxonomy"][0]


def test_the_confirmed_wipe_runs_without_an_error(db):
    at = AppTest.from_file(_DASHBOARD, default_timeout=60)
    at.switch_page("views/settings.py").run()
    assert not at.exception
    at.checkbox(key="wipe_taxonomy_ack").check().run()
    at.text_input(key="wipe_taxonomy_text").input("WIPE TAXONOMY").run()
    _wipe_button(at).click().run()
    assert not at.exception
    assert db.execute("SELECT COUNT(*) FROM parent_categories").fetchone()[0] == 0


def test_the_wipe_stays_disabled_until_it_is_confirmed(db):
    at = AppTest.from_file(_DASHBOARD, default_timeout=60)
    at.switch_page("views/settings.py").run()
    at.checkbox(key="wipe_taxonomy_ack").check().run()
    at.text_input(key="wipe_taxonomy_text").input("wipe").run()     # wrong case
    assert _wipe_button(at).disabled is True
