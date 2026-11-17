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

## Download and upload

On the Backups tab you can **download** any backup as a zip, to keep it somewhere else, and **add** a
backup from a zip. An added backup appears in the list, next to the ones taken here. A zip must hold a
`manifest.json` and `transactions_raw.parquet`, and nothing but `.parquet` tables. Adding a backup
doesn't restore it.

