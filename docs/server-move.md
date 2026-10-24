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

The plan in one line: **move today's tables into Postgres exactly as they are,
switch over once they've been proven right, and only then tidy up the design.**
Each step below says what happens, why, and how you'll know it worked.

### Stage 1: move to Postgres without changing anything else

**1. Make the tests work on Postgres.**
The tests never use your real data: each one makes a brand-new empty database,
creates the tables, adds a few fake rows, checks the code, and throws it away.
Today that's a throwaway DuckDB file. This step lets them do the same with a
throwaway Postgres, which GitHub starts empty for each test run and deletes
afterwards. It's how we find out whether the code works on Postgres before it
goes anywhere near your data.
*You'll know it worked when* CI is green with Postgres in it.
*Live change:* none.

**2. Create today's tables on the data box, empty.**
The same tables as today's two DuckDBs, in the data box's Postgres, in the
`bank_enrichment` schema (a schema is just a folder inside the database). The
server and the PC each have a table called `taxonomy_proposals` and one called
`category_proposals`. One folder can't hold two tables with the same name, so
the server's two become `server_taxonomy_proposals` and `server_category_proposals`.
*You'll know it worked when* every table is listed, all empty.
*Live change:* none.

**3. Copy your data in, and check it.**
A script copies everything from both DuckDBs into those tables. It empties them
first, so it can be run as many times as we like. A second script compares old
and new, table by table and row by row.
*You'll know it worked when* the compare says "0 differences".
*Live change:* none. The DuckDBs are only read.

**4. Make the code able to use Postgres.**
The server and the PC scripts learn to talk to Postgres. A setting in each
machine's `config/.env` chooses which database they use: `DB_BACKEND=duckdb`
(as now) or `postgres`. It stays on `duckdb` for now, so nothing changes yet.
The PC reaches the data box through an SSH tunnel via the app box, using the key
you already have. Streamlit keeps its own queries, with DuckDB reading the
Postgres tables (`ATTACH`).
*You'll know it worked when* a second Streamlit, pointed at Postgres on another
port, shows the same numbers as your normal one.
*Live change:* none.

**5. Run both side by side for a week.**
At the end of every `process.py` run, the copy and compare from step 3 run
again. If they fail, that's logged, and the run itself still finishes normally.
Telegram tells you the result once a day.
*You'll know it worked when* you've had a week of "0 differences".
*Live change:* one extra step at the end of `process.py` that can't break it.

**6. Switch.**
Stop the scheduled task, do one last copy and compare, set `DB_BACKEND=postgres`
on the server and the PC, then start the task again. From here Postgres is the
real database, and the DuckDB files are left untouched as a fallback.
*You'll know it worked when* a penny transfer gets its card, your reply is
saved, the next run classifies it, and it shows in the dashboard.
*Live change:* **yes**. This is the real switch.
*If something's wrong:* copy Postgres back into the DuckDB files, so nothing
from after the switch is lost, then set `DB_BACKEND=duckdb` again.

**7. Remove the DuckDB code.**
After a quiet week, the DuckDB paths and the side-by-side step come out.
*Live change:* clean-up only.

### Stage 2: tidy up the design, one small change at a time

With everything in one database, the server and the PC can see each other's
tables, so most of the awkward parts can go. Each change is small and checked
on its own.

**8. Remove the syncing, one piece at a time.**
Taxonomy, quick categories, alerts, rule suggestions, proposals and finally the
queue each stop being copied between the server and the PC, and are read
directly instead. Their endpoints are deleted one by one.
*You'll know each one worked when* the thing it fed still works: the Rules page
dropdowns, the quick-tap buttons, an alert, a suggestion card, a proposal, a new
transaction reaching the dashboard.

**9. Categories by id.**
Transactions and rules point at a category's id instead of copying its name, so
renaming a category is one change rather than an update to every transaction.
*You'll know it worked when* you rename a category and every transaction follows.

**10. The history tables.**
Today a transaction is one row that gets overwritten when its context or
category changes, so the old values are lost. From here, each arrival, context
and label is a new row that's never overwritten (§4), and a `transactions` view
shows the latest of each, under today's column names, so the dashboards don't
notice the difference.
*You'll know it worked when* the dashboards look the same, and changing a
transaction's category leaves the old one in its history.

### Stage 3: backups of the raw data

**11. Raw data into bronze every night.**
The history tables from step 10 are dumped into bronze by the data box's nightly
job, and from there copied to B2 (§7).
*You'll know it worked when* new files appear in bronze, and in B2 the next night.

**12. A real test restore.**
Rebuild the data from B2 into a scratch copy and check it matches the live one.
Once it does, data-vault's README gets the real restore steps, and the old
DuckDB files can be deleted.
