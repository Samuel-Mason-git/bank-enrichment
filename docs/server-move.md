# Moving into the data vault

Status: **design, not built.** Written Oct 2026, for agreeing the shape before any code changes.

## 1. Why

Today there are two databases, one on the server and one on the PC, kept in step by
syncing:

```
Monzo → server (FastAPI, DuckDB) ⇄ /export, /sync-* ⇄ PC (process.py, classifier, Streamlit, DuckDB)
```

Most of the awkward parts of the code exist only for that syncing:

- `taxonomy`, `taxonomy_proposals`, `category_proposals`, `quick_categories` and
  `rule_suggestions` live in one DB, get pushed to the other, and track
  `local_id` / `collected` to sync decisions back.
- `webhook_queue`'s `pending → enriched → processed` lifecycle, `/export`,
  `/mark-processed` and the 5-day purge exist to hand rows to the PC.
- The real record lives on the PC, so if the PC's disk dies, so does the history,
  and backing up means stitching two DBs together.

## 2. The shape

**All the data lives in one Postgres, on the data box. The app server and the PC
both read and write it directly.**

```
Monzo / Telegram ─→ app box (current Oracle Micro, `ssh oracle`)
                     ├─ caddy   HTTPS
                     └─ api     FastAPI: webhooks, Telegram, rules page, OAuth
                          │  Oracle's private network (10.0.0.0/24)
                          ↓
                    data box (`ssh vault`, from the data-vault repo)
                     └─ postgres   database `vault`, schema `bank_enrichment`
                        + nightly backups: pg_dump, bronze, B2, Telegram alerts
                          ↑  SSH tunnel through the app box (step 1.6); Tailscale later, if wanted
                    PC (when it's on)
                     ├─ worker     today's process.py steps: rules, quick taps, classifier,
                     │             judge, proposals, summaries
                     └─ dashboard  Streamlit, local only as now
```

- Webhooks and Telegram replies write straight into Postgres, so nothing waits on
  the PC to be safe.
- When the PC is on, the worker classifies whatever is waiting and the dashboard
  reads and edits the same database. There's nothing to sync.
- Streamlit stays on the PC, so it still needs no login.
- Backups are the data box's job, not bank-enrichment's (§7).
- Cost: £0. It uses both free Micros, an SSH tunnel, and the data-vault buckets.

When the work outgrows this (always-on classification, more projects), the worker
and dashboard can move onto a server too. Nothing in the tables changes.

## 3. The database

Already running (data-vault PRs #3 and #4): Postgres 18 on the data box, database
`vault`. bank-enrichment has its own schema and login, `bank_enrichment`, which
can only see and change that schema. Connection details are in the data box's
`~/.vault-secrets/bank_enrichment.env` and the OneDrive vault, and go into each
machine's `config/.env` as `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and
`DB_PASSWORD`.

| From | Reaches it via | `DB_HOST` |
|---|---|---|
| App box | Oracle's private network. Already open, and tested: the security list allows 5432 from `10.0.0.0/24` only | `10.0.0.252` |
| PC (WSL) | An SSH tunnel through the app box (`ssh -L 5433:10.0.0.252:5432 oracle`, step 1.6), so Postgres sees it as the app box. Tailscale can replace it later; `pg_hba.conf` already accepts its range | `localhost`, port `5433` |

**Why Postgres and not DuckDB:** the app box and the PC write at the same time
(webhooks, worker runs, dashboard edits). DuckDB allows only one process to have a
file open for writing (it's already why `process.py` fails while Streamlit is open),
and it can't be reached over a network. DuckDB stays for what it's good at:
reading bronze files and ad-hoc analysis.

**What's moving:** on 24 Oct 2026 the local DB had 795 transactions
(15 May to 24 Oct), every one with its `raw_payload`, 769 of them labelled. That's
small enough to migrate in seconds, and to re-run the migration as often as needed.

## 4. Tables

All in schema `bank_enrichment`. The core idea: **what happened is appended, never
overwritten.** A transaction arriving, you giving it context, the LLM labelling it,
you correcting it: each one is a new row. "What a transaction looks like now" is a
view over the latest of each.

### Append-only (the history; these are what bronze backs up)

| Table | Columns | Written by |
|---|---|---|
| `transactions_raw` | `id` (Monzo tx id, PK), `payload` JSONB, `received_at` | Monzo webhook |
| `context_events` | `event_id` (serial), `txn_id`, `user_context`, `skipped`, `created_at_override`, `source` (`telegram` / `rule` / `dashboard` / `migration`), `set_at` | Telegram replies and taps, rules, both dashboards |
| `label_events` | `event_id` (serial), `txn_id`, `subcategory_id` (NULL = cleared), `source` (`llm` / `rule` / `quick_tap` / `manual` / `judge` / `proposal` / `migration`), `model`, `confidence`, `set_at` | Classifier, rules, quick taps, judge, approved proposals, dashboard edits |

Every event stores the **full** value, not a change, so "latest wins" is always right.

### The view everything reads

`transactions`: one row per `transactions_raw` row, with the Monzo fields pulled
out of the payload, plus the latest context event, plus the latest label event
joined to its category names. **It keeps today's column names** (`llm_category`,
`llm_subcategory`, `user_context`, `skipped`, `created_at`...), so most dashboard
and summary queries keep working unchanged. `transaction_roles` sits on top of it
as it does now.

Two helper views replace the queue states:
- `needs_context`: raw rows with no context event yet (drives the Telegram card and follow-ups).
- `needs_label`: context set, not skipped, no label, not locked behind a proposal (drives the classifier).

### Edited in place (settings, not history)

| Table | Change from today |
|---|---|
| `parent_categories`, `subcategories` | Labels now point at `subcategories.id`, so **renaming a category is one row**, not an `UPDATE` across every transaction. Plus an `archived_at` column (see §5) |
| `category_roles` | Unchanged |
| `rules` | `category` / `subcategory` names → `subcategory_id` |
| `rule_suggestions`, `quick_categories` | One copy, pointing at `subcategory_id` |
| `taxonomy_proposals`, `category_proposals` | One copy each. `local_id` and the `collected` status go away. The proposal lock moves from `transactions.pending_category_proposal_id` to a `proposal_transactions (proposal_id, txn_id)` table |
| `judge_reviews`, `monthly_summaries`, `weekly_summaries`, `subscriptions` | Unchanged |
| `context_requests` | New, small: `txn_id`, `request_count`, `last_requested_at`, for Telegram follow-ups (today on `webhook_queue`) |

### Removed

`webhook_queue`, the server `taxonomy` mirror, `stats` (all counts come from the
data), the local `backups/` machinery (the data box backs up nightly), and the
endpoints `/export`, `/mark-processed`, `/sync-quick-categories`, `/sync-taxonomy`,
`/sync-taxonomy-proposals`, `/taxonomy-decisions*`, `/sync-category-proposals`,
`/category-decisions*`, `/sync-rule-suggestions` and `/send-alert`. The worker
reads and writes the tables directly, and calls Telegram itself for alerts.

**bank-enrichment provides the app; looking after the data is up to whoever runs
it.** It needs nothing more than a Postgres connection (`DB_*` in `.env`), and has
no backup schedule of its own. Here the data box handles that; someone else might
use their provider's backups. What the app keeps is an **optional** "back up
now" in Settings: a `pg_dump` of the project's schema to a file, for anyone who
wants one before a big change. It's never run automatically.

## 5. How a transaction flows

```
Monzo webhook ─→ transactions_raw
                 └─ rules ─→ context_event (auto-context / skip) and maybe label_event (fixed category)
              ─→ Telegram card (if it needs context)
Telegram reply / tap ─→ context_event (and a quick-tap label_event)
PC worker (scheduled, when the PC is on):
   needs_label ─→ classifier ─→ label_event (or a category proposal + lock)
   judge, proposals, rule suggestions, summaries (as today, minus the syncing)
Dashboard edit ─→ context_event / label_event (source = dashboard / manual)
Data box, nightly ─→ pg_dump, bronze dump, B2 copy
```

### What today's taxonomy edits become

| Today | Becomes |
|---|---|
| Rename a parent or subcategory (`UPDATE transactions SET llm_category = ...`) | Rename the row; no transaction writes |
| Move a subcategory to another parent | Change its `parent_id`; no transaction writes |
| Delete a subcategory, or wipe a parent's labels | A `label_event` with `subcategory_id = NULL` (`source = manual`) for each affected transaction, then archive the row, so old events still resolve to a name |
| Approved taxonomy "move" / "create" | `label_event`s (`source = proposal`) for exactly the evidence ids |
| Edit a transaction's date or context (`update_transaction_details`) | A `context_event` with `created_at_override` |

## 6. The code

- **Server (`src/server_scripts`)**: talks to Postgres directly (psycopg). Its
  queries are simple, so they're rewritten as plain Postgres SQL.
- **Worker and Streamlit (`src/local_scripts`)**: `dashboard.py` alone is about
  2,100 lines of DuckDB SQL. **For now they keep DuckDB as the query engine and
  `ATTACH` the Postgres database** (DuckDB's postgres extension), so most queries
  run unchanged against the `transactions` view. At this data size, speed isn't a
  concern. Rewriting them as plain Postgres SQL stays possible later, one query at
  a time.
- **Worker schedule**: unchanged. Task Scheduler runs it daily, and it can be run by hand at any time.
- **Tests**: the 906 tests use DuckDB fixtures. CI gets a Postgres service
  container, and the fixtures build the schema in it.
- **Dashboards**: both stay as they are. FastAPI's `/dashboard` stays on the app
  box behind its login, and Streamlit stays on the PC. Merging them is a later question.

## 7. Bronze and backups

The data box already does the generic part every night at 03:30: a `pg_dump` of
the whole database (7 days on the box, 30 in `bronze/_postgres/`), `rclone copy`
of bronze to B2 with a check, and Telegram alerts on failure plus an "all fine"
on Sundays.

What bank-enrichment adds is its **raw data in bronze**, so it can be rebuilt
without depending on Postgres or its dumps:

- **Append-only tables**: every row past the last exported `event_id`
  (`received_at` for `transactions_raw`) goes into one gzipped JSONL file per day:
  `bronze/bank-enrichment/<table>/<YYYY-MM-DD>.jsonl.gz`. Empty days write nothing.
- **Edited tables**: a snapshot to `bronze/bank-enrichment/config/<table>/<timestamp>.parquet`,
  only when the table's content differs from the last one dumped.
- **Where it runs**: in data-vault's nightly job, driven by a small per-project
  config listing each table and how it's dumped. bank-enrichment contains no
  backup code. The watermarks live in a data-vault table, not in bank-enrichment's schema.
- **Restore = rebuild**: a fresh schema plus a replay of bronze in order gives back
  everything. This is the real test restore from B2 that data-vault is still owed.

## 8. Rollout

The move happens in two stages: **first the engine, then the redesign.** Today's
tables go into Postgres exactly as they are, and the existing code is pointed at
them. Only once that's settled does the design above (§4 and §5) get built,
inside Postgres, as small migrations. The step-by-step list is §10, at the end.

## 9. Decided

- **Worker schedule:** unchanged (Task Scheduler, daily, plus by hand).
- **Streamlit's queries:** `ATTACH` Postgres from DuckDB for now (§6).
- **Backups:** not the app's job. An optional "back up now" `pg_dump` in Settings, never automatic (§4).
- **Dashboards:** both stay as they are. Once there's a server with the compute
  for it, both get server access, with Streamlit behind a login.

## 10. Step by step

Each step is small, has a check you can see, and says what changes live. Steps
marked *none* can be stopped at any point without the old setup noticing.

### Stage 1: same tables, new engine

During this stage the code works with either database, chosen by `DB_BACKEND`
(`duckdb` or `postgres`) in each machine's `config/.env`. It stays `duckdb`
until step 1.10, so going back is just a matter of flipping it.

Server and local both have tables called `taxonomy_proposals` and
`category_proposals`, so the two sides get a schema each: **`bank_enrichment`**
for today's local tables and **`bank_enrichment_server`** for the server's. Both
are owned by the `bank_enrichment` login, and none of the SQL changes.

| # | Step | Check | Live change |
|---|---|---|---|
| 1.1 | **Postgres in CI**: a Postgres service in the GitHub workflow, and test fixtures that can build the tables in it | CI is green, with Postgres running alongside | None |
| 1.2 | **Create both schemas** on the data box with today's tables in them, empty (`bank_enrichment_server` made with one admin command, recorded in data-vault's README) | `\dt` lists every table from `sql/tables.sql` and `sql/server_tables.sql` | None |
| 1.3 | **Copy script**: copies both DuckDBs into Postgres table by table, emptying first, so it can be re-run | Row counts match, table by table | None |
| 1.4 | **Compare script**: diffs every table, old against new, row by row | "0 differences" | None |
| 1.5 | **Server code on Postgres** (psycopg), behind `DB_BACKEND` | The server tests pass on both engines | None (it deploys, but stays on `duckdb`) |
| 1.6 | **SSH tunnel from the PC** through the app box (`ssh -L 5433:10.0.0.252:5432 oracle`) | The PC can query the data box | None |
| 1.7 | **Local code via `ATTACH`** (worker and Streamlit), behind `DB_BACKEND` | The local tests pass on both engines | None |
| 1.8 | **Second Streamlit on Postgres**, on another port | Open both side by side: the same numbers everywhere | None |
| 1.9 | **Parallel run**: copy + compare at the end of every `process.py` run, logged only, never failing the run; the result on Telegram daily | A week of daily "0 differences" | One logged-only step in `process.py` |
| 1.10 | **The switch**: stop the scheduled task, run a final copy + compare, flip `DB_BACKEND=postgres` on the server (restart) and the PC, start the scheduled task again | A penny transfer gets its card, a reply lands, a run classifies it, and both dashboards show it | **Yes**: Postgres is now the real database |
| 1.11 | **Rollback window**: the old DuckDB files are frozen, not deleted. If something's wrong, the copy script runs in reverse (Postgres → DuckDB, so nothing since the switch is lost) and `DB_BACKEND` flips back | — | Only if needed |
| 1.12 | **Remove the DuckDB code paths** and the parallel-run step, after a quiet week | The tests pass on Postgres only | Clean-up |

### Stage 2: the redesign, inside Postgres

Each step is a migration plus the code that goes with it, small enough to check
and to revert on its own. While a step's old and new forms both exist, the code
writes both and reads the new one, until the check passes and the old form is
removed.

| # | Step | Check | Live change |
|---|---|---|---|
| 2.1 | **Taxonomy:** the server reads the local category tables directly; `/sync-taxonomy` and the server `taxonomy` copy go | The Rules page dropdowns still list every category | Small |
| 2.2 | **Quick categories:** the server works them out from transactions directly; `/sync-quick-categories` goes | Cards show the same quick-tap buttons as before | Small |
| 2.3 | **Alerts:** the worker sends Telegram alerts itself; `/send-alert` goes | A test alert arrives | Small |
| 2.4 | **Rule suggestions:** written straight into the server's table; `/sync-rule-suggestions` goes | The next suggestion card appears as normal | Small |
| 2.5 | **Taxonomy and category proposals:** one copy each, decisions read directly; `local_id`, `collected` and four endpoints go | Approve one proposal end to end | Small |
| 2.6 | **The queue:** `process.py` reads `webhook_queue` directly; `/export` and `/mark-processed` go | A new transaction reaches the dashboard as before | Small |
| 2.7 | **The two schemas merge into one** (`bank_enrichment`), now that nothing is duplicated | All tests pass; data-vault's README updated | Small |
| 2.8 | **Categories by id:** `subcategory_id` added alongside the names, filled in, readers switched, then the name columns dropped | Rename a category: every transaction follows, with no mass `UPDATE` | Small |
| 2.9 | **`transactions_raw`** filled from the webhook, with existing rows backfilled from `raw_payload` | Its count equals `transactions` | Small |
| 2.10 | **`context_events`** written alongside today's context columns, backfilled | The latest event per transaction equals today's column, for every row | Small |
| 2.11 | **`label_events`** written alongside today's label columns, backfilled | The latest event per transaction equals today's label, for every row | Small |
| 2.12 | **The `transactions` view** replaces the table: readers switch to it, then the old wide table and `webhook_queue` go | Both dashboards unchanged; a full day of normal use | Yes (the last big one) |
| 2.13 | **`stats` goes**: its counts come from the data | The FastAPI dashboard's numbers unchanged | Small |
| 2.14 | **Optional "back up now"** in Settings (a schema `pg_dump`), replacing the local `backups/` machinery | A dump file appears and restores into a scratch schema | Small |

### Stage 3: backups of the raw data

| # | Step | Check | Live change |
|---|---|---|---|
| 3.1 | **bank-enrichment's bronze config** in data-vault: the event tables dumped daily, edited tables snapshotted when they change (§7) | Files appear in bronze, then B2 the next night | None for the app |
| 3.2 | **Test restore from B2**: rebuild the schema from bronze into a scratch schema | It matches the live one; data-vault's README Restore section gets the real steps | None |
| 3.3 | **Delete the old DuckDB files** | — | Clean-up |
