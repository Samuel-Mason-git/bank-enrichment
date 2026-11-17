"""The Storage tab: what's kept where, from config/.env, with the bronze copy testable from the page.
No passwords or keys are shown."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

import export_destinations as ed

_DASHBOARD = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def _page(db):
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.switch_page("views/settings.py").run()
    assert not at.exception
    return at


def test_with_no_bronze_copy_the_page_says_it_is_off(db, monkeypatch):
    monkeypatch.delenv("BRONZE_DESTINATION", raising=False)
    at = _page(db)
    assert any("Off. Set BRONZE_DESTINATION" in i.value for i in at.info)


def test_a_folder_copy_is_described_and_tested(db, monkeypatch, tmp_path):
    monkeypatch.setenv("BRONZE_DESTINATION", "folder")
    monkeypatch.setenv("BRONZE_FOLDER", str(tmp_path / "bronze"))
    at = _page(db)
    at.button(key="storage_test_bronze").click().run()
    assert not at.exception
    assert any("takes a copy" in s.value for s in at.success)
    assert (tmp_path / "bronze").is_dir()


def test_a_bad_folder_is_reported_on_the_page(db, monkeypatch):
    monkeypatch.setenv("BRONZE_DESTINATION", "folder")
    monkeypatch.setenv("BRONZE_FOLDER", "relative/bronze")
    at = _page(db)
    at.button(key="storage_test_bronze").click().run()
    assert any("full path" in e.value for e in at.error)


def test_the_page_never_shows_a_secret(db, monkeypatch):
    monkeypatch.setenv("BRONZE_DESTINATION", "s3")
    monkeypatch.setenv("BRONZE_S3_ENDPOINT", "https://acct.r2.example")
    monkeypatch.setenv("BRONZE_S3_BUCKET", "copies")
    monkeypatch.setenv("BRONZE_S3_PREFIX", "bank-enrichment")
    monkeypatch.setenv("BRONZE_S3_KEY_ID", "KEYID-SHOULD-NOT-SHOW")
    monkeypatch.setenv("BRONZE_S3_SECRET", "SECRET-SHOULD-NOT-SHOW")
    monkeypatch.setenv("DB_PASSWORD", "DBPASS-SHOULD-NOT-SHOW")
    at = _page(db)
    shown = " ".join(m.value for m in at.markdown) + " " + " ".join(i.value for i in at.info) \
        + " " + " ".join(c.value for c in at.caption)
    for secret in ("KEYID-SHOULD-NOT-SHOW", "SECRET-SHOULD-NOT-SHOW", "DBPASS-SHOULD-NOT-SHOW"):
        assert secret not in shown


def test_describe_leaves_out_the_secrets(db):
    dest = ed.Destination(kind="s3", endpoint="https://x", bucket="b", prefix="p", key_id="KEY", secret="SECRET")
    info = ed.describe(dest)
    assert set(info) == {"kind", "folder", "endpoint", "bucket", "prefix", "keep", "last_day"}
    assert "KEY" not in str(info) and "SECRET" not in str(info)
