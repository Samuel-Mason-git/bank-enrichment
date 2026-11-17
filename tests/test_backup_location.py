"""Where backups are saved: the folder chosen on the Settings page, then BACKUP_DIR, then the default."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import database_functions as dbf
from database_functions import BackupDirError, backup_dir, reset_backup_dir, set_backup_dir

_DASHBOARD = str(Path(__file__).parent.parent / "src" / "dashboard" / "dashboard.py")


def test_a_chosen_folder_is_used_and_created(db, tmp_path):
    folder = tmp_path / "my backups"
    assert set_backup_dir(str(folder)) == folder
    assert folder.is_dir()
    assert backup_dir() == folder


def test_backups_go_into_the_chosen_folder(db, tmp_path):
    folder = tmp_path / "chosen"
    set_backup_dir(str(folder))
    taken = dbf.backup_db("manual")
    assert taken.parent == folder
    assert (taken / "transactions_raw.parquet").exists()


def test_without_a_choice_BACKUP_DIR_is_used(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "from-env"))
    assert backup_dir() == tmp_path / "from-env"


def test_without_a_choice_or_BACKUP_DIR_the_default_is_used(db, monkeypatch):
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    assert backup_dir() == dbf.default_backup_dir()


def test_the_default_clears_the_choice(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "from-env"))
    set_backup_dir(str(tmp_path / "chosen"))
    reset_backup_dir()
    assert backup_dir() == tmp_path / "from-env"


def test_a_relative_path_is_refused_and_nothing_changes(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "from-env"))
    with pytest.raises(BackupDirError, match="full path"):
        set_backup_dir("relative/folder")
    assert backup_dir() == tmp_path / "from-env"


def test_a_windows_path_is_refused_with_the_wsl_form(db):
    with pytest.raises(BackupDirError, match="/mnt/c"):
        set_backup_dir(r"C:\Users\mason\Backups")


def test_an_empty_path_is_refused(db):
    with pytest.raises(BackupDirError, match="Type the folder"):
        set_backup_dir("   ")
