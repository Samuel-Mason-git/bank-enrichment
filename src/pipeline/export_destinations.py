"""Where a copy of the data goes each day, besides the database: nowhere (the default),
a folder on this machine, or an S3-compatible bucket (Cloudflare R2, Backblaze B2,
Oracle Object Storage, AWS).

Each copy is one folder: a gzipped JSON Lines file per table, and a manifest. That's
the shape data-vault's bronze uses, and it can be read without this app. The worker
doesn't carry pandas, so the tables are read with psycopg.

Safety:
  - Nothing is written unless BRONZE_DESTINATION says where.
  - A destination needs its own prefix, and the prefix can't be bronze, or sit under
    bronze/: that's where a setup that already writes its own copies keeps them.
  - After each copy, every file is checked to be there at the same size.
  - Only the newest BRONZE_KEEP copies are kept, and only under this destination.
  - A day is exported once. A failure is tried again on the next run.
"""
import gzip
import json
import tempfile
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import app_config
from database_functions import BACKUP_TABLES, get_con, get_setting, now, set_setting

log = logging.getLogger(__name__)

LAST_DAY_SETTING = "bronze_last_day"
COPY_NAME = re.compile(r"^\d{8}-\d{6}_export(-\d+)?$")
MANIFEST = "manifest.json"
RESERVED_PREFIXES = ("bronze",)


class ExportError(Exception):
    """A destination that can't be used, or a copy that didn't check out."""


@dataclass(frozen=True)
class Destination:
    kind: str                      # "none", "folder" or "s3"
    folder: str | None = None
    endpoint: str | None = None
    bucket: str | None = None
    prefix: str | None = None
    key_id: str | None = None
    secret: str | None = None
    keep: int = 10


def load() -> Destination:
    """The destination from config/.env. Nothing set means none."""
    kind = (app_config.get("BRONZE_DESTINATION", "none") or "none").strip().lower()
    keep = int(app_config.get("BRONZE_KEEP", "10") or 10)
    return Destination(
        kind=kind,
        folder=app_config.get("BRONZE_FOLDER"),
        endpoint=app_config.get("BRONZE_S3_ENDPOINT"),
        bucket=app_config.get("BRONZE_S3_BUCKET"),
        prefix=(app_config.get("BRONZE_S3_PREFIX") or "").strip("/") or None,
        key_id=app_config.get("BRONZE_S3_KEY_ID"),
        secret=app_config.get("BRONZE_S3_SECRET"),
        keep=keep,
    )


def validate(dest: Destination) -> None:
    """Refuse a destination that's incomplete or would write into someone else's space."""
    if dest.kind == "none":
        return
    if dest.keep < 1:
        raise ExportError("BRONZE_KEEP must be at least 1.")
    if dest.kind == "folder":
        if not dest.folder or not Path(dest.folder).is_absolute():
            raise ExportError("BRONZE_FOLDER must be a full path.")
        return
    if dest.kind == "s3":
        missing = [name for name, value in (
            ("BRONZE_S3_ENDPOINT", dest.endpoint), ("BRONZE_S3_BUCKET", dest.bucket),
            ("BRONZE_S3_PREFIX", dest.prefix), ("BRONZE_S3_KEY_ID", dest.key_id),
            ("BRONZE_S3_SECRET", dest.secret)) if not value]
        if missing:
            raise ExportError("Missing for the bucket: " + ", ".join(missing))
        if not dest.endpoint.startswith("https://"):
            raise ExportError("BRONZE_S3_ENDPOINT must start with https://")
        first = dest.prefix.split("/")[0]
        if first in RESERVED_PREFIXES:
            raise ExportError(f"The prefix can't start with {first}/: that's kept for another copy of the data.")
        return
    raise ExportError(f"BRONZE_DESTINATION must be none, folder or s3, not {dest.kind!r}.")


def _stamp(when: datetime) -> str:
    return f"{when:%Y%m%d-%H%M%S}_export"


def stage(con, staging: Path, when: datetime) -> Path:
    """Write the tables, gzipped as JSON Lines, into a new folder under staging. Returns it."""
    folder = staging / _stamp(when)
    folder.mkdir(parents=True)
    counts = {}
    for table in BACKUP_TABLES:
        n = 0
        with gzip.open(folder / f"{table}.jsonl.gz", "wt", encoding="utf-8") as out:
            with con.cursor() as cur:
                cur.execute(f"SELECT row_to_json(t) FROM {table} t")
                for (row,) in cur:
                    out.write(json.dumps(row, default=str) + "\n")
                    n += 1
        counts[table] = n
    (folder / MANIFEST).write_text(json.dumps(
        {"created": when.isoformat(), "tables": counts, "rows": sum(counts.values())}, indent=2), encoding="utf-8")
    return folder


def _sizes(folder: Path) -> dict[str, int]:
    return {p.name: p.stat().st_size for p in folder.iterdir() if p.is_file()}


# ── a folder ───────────────────────────────────────────────────────────────

def _free_name(name: str, taken) -> str:
    """The name, or the name with -2, -3… after it, whichever nothing else has taken."""
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{name}-{n}"
    return candidate


def copy_to_folder(dest: Destination, staged: Path) -> Path:
    root = Path(dest.folder)
    root.mkdir(parents=True, exist_ok=True)
    target = root / _free_name(staged.name, {p.name for p in root.iterdir()})
    shutil.copytree(staged, target)
    if _sizes(target) != _sizes(staged):
        shutil.rmtree(target, ignore_errors=True)
        raise ExportError(f"The copy in {target} doesn't match what was staged.")
    kept = sorted((p for p in root.iterdir() if p.is_dir() and COPY_NAME.match(p.name)), reverse=True)
    for old in kept[dest.keep:]:
        shutil.rmtree(old, ignore_errors=True)
    return target


# ── a bucket ───────────────────────────────────────────────────────────────

def s3_client(dest: Destination):
    """The S3 client for the bucket. boto3 is imported here, so a destination that
    isn't a bucket never needs it."""
    import boto3
    return boto3.client("s3", endpoint_url=dest.endpoint, aws_access_key_id=dest.key_id,
                        aws_secret_access_key=dest.secret, region_name="auto")


def _list(client, bucket: str, prefix: str) -> dict[str, int]:
    found = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            found[obj["Key"]] = obj["Size"]
    return found


def copy_to_bucket(dest: Destination, staged: Path, client) -> str:
    taken = {k.split("/")[1] for k in _list(client, dest.bucket, f"{dest.prefix}/") if k.count("/") >= 2}
    key_root = f"{dest.prefix}/{_free_name(staged.name, taken)}/"
    for path in sorted(staged.iterdir()):
        client.upload_file(str(path), dest.bucket, key_root + path.name)
    uploaded = _list(client, dest.bucket, key_root)
    expected = {key_root + name: size for name, size in _sizes(staged).items()}
    if uploaded != expected:
        raise ExportError(f"The bucket doesn't hold the copy that was sent (checked {key_root}).")
    _prune_bucket(dest, client)
    return key_root


def _prune_bucket(dest: Destination, client) -> None:
    """Keep the newest copies under this prefix; delete the rest. Only keys under this prefix
    and named like a copy are touched."""
    root = f"{dest.prefix}/"
    copies = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=dest.bucket, Prefix=root, Delimiter="/"):
        for common in page.get("CommonPrefixes", []):
            name = common["Prefix"][len(root):].rstrip("/")
            if COPY_NAME.match(name):
                copies.add(name)
    for old in sorted(copies, reverse=True)[dest.keep:]:
        for key in _list(client, dest.bucket, f"{root}{old}/"):
            client.delete_object(Bucket=dest.bucket, Key=key)


# ── putting it together ────────────────────────────────────────────────────

def export(dest: Destination, con=None, client=None, when: datetime | None = None,
           staging: Path | None = None) -> str:
    """One copy to the destination. Returns where it went. Raises ExportError if it didn't check out."""
    validate(dest)
    con = con or get_con()
    when = when or now()
    with tempfile.TemporaryDirectory(dir=staging) as tmp:
        staged = stage(con, Path(tmp), when)
        if dest.kind == "folder":
            return str(copy_to_folder(dest, staged))
        client = client or s3_client(dest)
        return f"s3://{dest.bucket}/{copy_to_bucket(dest, staged, client)}"


def nightly(today, con=None, dest: Destination | None = None, client=None) -> str | None:
    """Once a day: export if a destination is set and today's copy hasn't gone yet. Returns where
    it went, or None if there's nothing to do. A failure is logged and tried again next run."""
    dest = dest or load()
    if dest.kind == "none":
        return None
    if get_setting(LAST_DAY_SETTING) == today.isoformat():
        return None
    try:
        where = export(dest, con=con, client=client)
    except Exception as e:
        log.error(f"Export to {dest.kind} didn't complete: {type(e).__name__}: {e}")
        return None
    set_setting(LAST_DAY_SETTING, today.isoformat())
    log.info(f"Exported today's copy to {where}")
    return where
