# Backups

[← Back to the README](../../README.md)

The database is yours to back up, however you run it: this project doesn't include backups. Every change to a transaction is kept in its history anyway.

**Back up now** in the Settings page takes a copy of your own before you try something: every
table saved as a Parquet file, in a folder named `<timestamp>_<reason>` in the backup folder
(default `~/bank-enrichment-backups`). Nothing is ever backed up automatically. Read one back
with DuckDB or pandas, e.g.

```sql
SELECT * FROM '~/bank-enrichment-backups/20261025-220341_manual/transactions_raw.parquet';
```

Each copy carries a `manifest.json` recording what it captured, so the Settings page can tell you
what's in it: transaction count, how many were classified, taxonomy size, subscription count,
the date range covered, and size on disk. The most recent 10 are kept and older ones pruned;
any can also be deleted by hand from the same section (behind a confirmation).

## Download

On the Backups tab you can **download** any backup as a zip, to keep it somewhere else. The zip holds the
backup's files at the top level: its `.parquet` tables and `manifest.json`.

## A daily copy somewhere else

Besides the database, the app can put a copy of its data somewhere each day. It's **off by default**:
nothing is written unless `EXPORT_DESTINATION` says where.

| `EXPORT_DESTINATION` | Where | Needs |
|---|---|---|
| `none` (default) | Nowhere | Nothing |
| `folder` | A folder on the machine running the worker | `EXPORT_FOLDER`, a full path |
| `s3` | An S3-compatible bucket: Cloudflare R2, Backblaze B2, Oracle Object Storage, AWS | `EXPORT_S3_ENDPOINT` (https), `EXPORT_S3_BUCKET`, `EXPORT_S3_PREFIX`, `EXPORT_S3_KEY_ID`, `EXPORT_S3_SECRET` |

Each copy is one folder (`<date>-<time>_export`): a gzipped JSON Lines file per table, plus a
`manifest.json` with the row counts. That's readable without this app. The copy is written once a day,
on the first worker run after 08:00 (the worker doesn't run overnight).

- **Checked:** after each copy, every file is confirmed to be there at the same size. A failure is tried
  again on the next run.
- **Kept:** the newest `EXPORT_KEEP` copies (10 by default). Only copies under this destination are removed.
- **Safe prefix:** the bucket prefix can't be `bronze`, or sit under `bronze/`, which is where a setup that
  already writes its own copies keeps them.

The setup command asks where the copy should go, and tests the bucket by writing and removing a test file
before it saves anything. The bucket keys are kept in `config/.env` with the rest of the secrets.
