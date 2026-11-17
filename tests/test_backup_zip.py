"""Downloading a backup as a zip."""
import io
import zipfile

import database_functions as dbf
from database_functions import backup_db, backup_zip


def test_a_backup_downloads_as_a_zip_with_its_files(db):
    original = backup_db("manual")
    data = backup_zip(original)
    names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert names == {p.name for p in original.iterdir() if p.is_file()}
    assert "manifest.json" in names and "transactions_raw.parquet" in names


def test_the_zip_has_no_folders_inside(db):
    original = backup_db("manual")
    for name in zipfile.ZipFile(io.BytesIO(backup_zip(original))).namelist():
        assert "/" not in name and "\\" not in name
