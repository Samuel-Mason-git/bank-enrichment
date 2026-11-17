"""Downloading a backup as a zip, and adding one from a zip."""
import io
import json
import zipfile

import pytest

import database_functions as dbf
from database_functions import BackupZipError, add_backup_zip, backup_dir, backup_db, backup_zip


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_a_backup_round_trips_through_a_zip(db, tmp_path):
    original = backup_db("manual")
    data = backup_zip(original)
    names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert "manifest.json" in names and "transactions_raw.parquet" in names

    added = add_backup_zip(data, name="from laptop")
    assert added.parent == backup_dir()
    assert added.name.endswith("_from-laptop")
    assert sorted(p.name for p in added.iterdir()) == sorted(p.name for p in original.iterdir())
    assert json.loads((added / "manifest.json").read_text()) == json.loads((original / "manifest.json").read_text())


def test_a_zip_without_a_manifest_is_refused(db):
    with pytest.raises(BackupZipError, match="manifest.json"):
        add_backup_zip(_zip({"transactions_raw.parquet": b"x"}))


def test_a_zip_without_the_transactions_is_refused(db):
    with pytest.raises(BackupZipError, match="transactions_raw"):
        add_backup_zip(_zip({"manifest.json": b"{}"}))


def test_a_path_inside_the_zip_is_refused(db):
    with pytest.raises(BackupZipError, match="Unexpected file"):
        add_backup_zip(_zip({"manifest.json": b"{}", "transactions_raw.parquet": b"x",
                             "../evil.parquet": b"x"}))


def test_a_file_that_isnt_a_table_is_refused(db):
    with pytest.raises(BackupZipError, match="Unexpected file"):
        add_backup_zip(_zip({"manifest.json": b"{}", "transactions_raw.parquet": b"x", "run.exe": b"x"}))


def test_not_a_zip_is_refused(db):
    with pytest.raises(BackupZipError, match="isn't a zip"):
        add_backup_zip(b"hello")


def test_a_manifest_that_isnt_json_is_refused(db):
    with pytest.raises(BackupZipError, match="isn't valid"):
        add_backup_zip(_zip({"manifest.json": b"{not json", "transactions_raw.parquet": b"x"}))


def test_a_refused_zip_adds_nothing(db):
    before = set(dbf.list_backups())
    with pytest.raises(BackupZipError):
        add_backup_zip(_zip({"manifest.json": b"{}"}))
    assert set(dbf.list_backups()) == before
