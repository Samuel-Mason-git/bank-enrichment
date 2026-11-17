"""The backup folder is chosen by clicking through folders, and the Danger Zone is marked as dangerous."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

from database_functions import backup_dir

_DASHBOARD = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def _page():
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.switch_page("views/settings.py").run()
    assert not at.exception
    return at


def test_the_folder_browser_saves_the_folder_you_open(db):
    root = backup_dir()
    (root / "Google Drive").mkdir(parents=True)
    at = _page()
    at.button(key="backup_browse_into_" + str(root / "Google Drive")).click().run()
    at.button(key="backup_browse_use").click().run()
    assert not at.exception
    assert backup_dir() == root / "Google Drive"


def test_a_new_folder_can_be_made_and_used(db):
    root = backup_dir()
    root.mkdir(parents=True)
    at = _page()
    at.text_input(key="backup_browse_new").input("Weekly").run()
    at.button(key="backup_browse_create").click().run()
    at.button(key="backup_browse_use").click().run()
    assert backup_dir() == root / "Weekly"
    assert (root / "Weekly").is_dir()


def test_the_danger_zone_is_marked_as_dangerous(db):
    at = _page()
    assert any("Danger zone" in e.value and "can't be undone" in e.value for e in at.error)
