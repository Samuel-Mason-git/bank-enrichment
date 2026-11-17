"""Where a daily copy goes: a folder or a bucket, off by default, checked after each copy, and kept
to a number of copies, only under its own prefix."""
import gzip
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import database_functions as dbf
import export_destinations as ed
from tests.local_helpers import insert_transaction


class FakeS3:
    """Just enough of boto3's client: upload_file, paginated listing with delimiters, delete_object."""

    def __init__(self):
        self.objects = {}

    def upload_file(self, path, bucket, key):
        self.objects[key] = Path(path).stat().st_size

    def get_paginator(self, name):
        objects = self.objects

        class Paginator:
            def paginate(self, Bucket, Prefix="", Delimiter=None):
                keys = sorted(k for k in objects if k.startswith(Prefix))
                if not Delimiter:
                    yield {"Contents": [{"Key": k, "Size": objects[k]} for k in keys]}
                    return
                common = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter
                                 for k in keys if Delimiter in k[len(Prefix):]})
                yield {"CommonPrefixes": [{"Prefix": c} for c in common]}

        return Paginator()

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def _dest(**kw):
    base = dict(kind="s3", endpoint="https://s3.example", bucket="copies", prefix="bank-enrichment",
                key_id="k", secret="s", keep=10)
    base.update(kw)
    return ed.Destination(**base)


# ── the rules ──────────────────────────────────────────────────────────────

def test_nothing_set_means_no_copy(monkeypatch):
    for name in ("EXPORT_DESTINATION", "EXPORT_FOLDER"):
        monkeypatch.delenv(name, raising=False)
    dest = ed.load()
    assert dest.kind == "none"
    ed.validate(dest)


def test_a_folder_must_be_a_full_path(tmp_path):
    with pytest.raises(ed.ExportError, match="full path"):
        ed.validate(ed.Destination(kind="folder", folder="relative/copies"))


def test_a_bucket_needs_every_setting():
    with pytest.raises(ed.ExportError, match="EXPORT_S3_SECRET"):
        ed.validate(_dest(secret=None))


def test_a_bucket_needs_https():
    with pytest.raises(ed.ExportError, match="https"):
        ed.validate(_dest(endpoint="http://s3.example"))


@pytest.mark.parametrize("prefix", ["bronze", "bronze/bank-enrichment"])
def test_the_bronze_namespace_is_refused(prefix):
    with pytest.raises(ed.ExportError, match="bronze"):
        ed.validate(_dest(prefix=prefix))


def test_a_prefix_that_only_starts_like_bronze_is_fine():
    ed.validate(_dest(prefix="bronze-tests/me"))


# ── a folder ───────────────────────────────────────────────────────────────

def test_a_folder_copy_has_every_table_and_a_manifest(db, tmp_path):
    insert_transaction(db, "tx1", amount=-12.5, merchant_name="Corner Shop")
    dest = ed.Destination(kind="folder", folder=str(tmp_path / "copies"))
    where = Path(ed.export(dest, when=datetime(2026, 11, 17, 8, 0)))
    assert where.name == "20261117-080000_export"
    manifest = json.loads((where / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tables"]["transactions_raw"] == 1
    with gzip.open(where / "transactions_raw.jsonl.gz", "rt", encoding="utf-8") as f:
        assert "tx1" in f.readline()


def test_only_the_newest_copies_are_kept(db, tmp_path):
    dest = ed.Destination(kind="folder", folder=str(tmp_path / "copies"), keep=2)
    for day in (1, 2, 3):
        ed.export(dest, when=datetime(2026, 11, day, 8, 0))
    kept = sorted(p.name for p in (tmp_path / "copies").iterdir())
    assert kept == ["20261102-080000_export", "20261103-080000_export"]


def test_folders_that_arent_copies_are_left_alone(db, tmp_path):
    root = tmp_path / "copies"
    (root / "my-notes").mkdir(parents=True)
    dest = ed.Destination(kind="folder", folder=str(root), keep=1)
    ed.export(dest, when=datetime(2026, 11, 1, 8, 0))
    ed.export(dest, when=datetime(2026, 11, 2, 8, 0))
    assert (root / "my-notes").is_dir()


# ── a bucket ───────────────────────────────────────────────────────────────

def test_a_bucket_copy_is_uploaded_under_the_prefix_and_checked(db):
    s3 = FakeS3()
    where = ed.export(_dest(), when=datetime(2026, 11, 17, 8, 0), client=s3)
    assert where == "s3://copies/bank-enrichment/20261117-080000_export/"
    keys = set(s3.objects)
    assert "bank-enrichment/20261117-080000_export/manifest.json" in keys
    assert "bank-enrichment/20261117-080000_export/transactions_raw.jsonl.gz" in keys


def test_a_bucket_keeps_only_the_newest_copies_under_its_prefix(db):
    s3 = FakeS3()
    s3.objects["bank-enrichment-other/keep.txt"] = 1           # another prefix: not ours to touch
    dest = _dest(keep=2)
    for day in (1, 2, 3):
        ed.export(dest, when=datetime(2026, 11, day, 8, 0), client=s3)
    copies = {k.split("/")[1] for k in s3.objects if k.startswith("bank-enrichment/")}
    assert copies == {"20261102-080000_export", "20261103-080000_export"}
    assert "bank-enrichment-other/keep.txt" in s3.objects


def test_a_copy_the_bucket_doesnt_hold_is_an_error(db):
    class Lossy(FakeS3):
        def upload_file(self, path, bucket, key):
            pass                                                # uploads nothing
    with pytest.raises(ed.ExportError, match="doesn't hold"):
        ed.export(_dest(), when=datetime(2026, 11, 17, 8, 0), client=Lossy())


# ── once a day ─────────────────────────────────────────────────────────────

def test_a_day_is_exported_once(db, monkeypatch, tmp_path):
    monkeypatch.setenv("EXPORT_DESTINATION", "folder")
    monkeypatch.setenv("EXPORT_FOLDER", str(tmp_path / "copies"))
    today = date(2026, 11, 17)
    assert ed.nightly(today) is not None
    assert ed.nightly(today) is None
    assert ed.nightly(today + timedelta(days=1)) is not None


def test_a_failed_copy_is_tried_again_on_the_next_run(db, monkeypatch):
    monkeypatch.setenv("EXPORT_DESTINATION", "folder")
    monkeypatch.setenv("EXPORT_FOLDER", "relative/not-allowed")
    today = date(2026, 11, 17)
    assert ed.nightly(today) is None
    assert dbf.get_setting(ed.LAST_DAY_SETTING) is None      # not marked done, so it's tried again
