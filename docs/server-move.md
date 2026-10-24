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
                          ↑  Tailscale (not set up yet; needed by step 4)
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
- Cost: £0. It uses both free Micros, Tailscale's free plan, and the data-vault buckets.

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
| PC (WSL) | Tailscale. The data box's `pg_hba.conf` already accepts the Tailscale range | The data box's Tailscale address |

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

The Settings tab's "back up before anything irreversible" can stay as a quick
`pg_dump` of the schema if that safety net is still wanted. That's decided at step 5.

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
  2,100 lines of DuckDB SQL. Two ways to point it at Postgres:
  1. Rewrite the queries for Postgres. This is the cleaner end state, and the most work.
  2. Keep DuckDB as the query engine and `ATTACH` the Postgres database
     (DuckDB's postgres extension). Most queries then run unchanged against the
     `transactions` view. At this data size, speed isn't a concern.

  Decided at step 5, after seeing how the server and worker changes go.
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

## 8. Moving the data

1. The schema is created in `bank_enrichment` on the data box. It starts empty;
   the current setup keeps running untouched.
2. A one-off migration script, run from the PC (or on the app box, with the local
   DB copied across), reads both current DuckDBs:
   - local `transactions.raw_payload` → `transactions_raw`
   - current `user_context` / `skipped` → one `context_event` each (`source = migration`)
   - current `llm_*` → one `label_event` each, keeping `llm_model` and `classified_at`
   - taxonomy, roles, rules, proposals, judge reviews and summaries copied across,
     with names mapped to ids. **Any name that doesn't map is listed, not guessed.**
   - anything still only in the server's `webhook_queue` (arrived but not yet
     pulled to the PC) comes across too.

   It empties the schema first, so it can be re-run any number of times.
3. **Check:** monthly totals by role, category counts and row counts must match
   the current Streamlit dashboard exactly. Repeat until they do.
4. **Switch:** stop the PC's scheduled task, run the migration once more to catch
   anything new, then merge the server PR (it deploys itself). Same server, same
   URLs: nothing to re-register with Monzo or Telegram. Monzo retries anything that
   arrives during the minute of the deploy.
5. Start the new PC worker in the scheduled task's place.
6. Keep the old DuckDB files untouched for a couple of weeks before deleting anything.

## 9. Order of work

Each step is its own PR, and nothing changes for real until step 6.

1. The schema and views; the migration script; the comparison check. Run against
   the data box from the app box, which can already reach it.
2. The server app on Postgres (webhooks, Telegram, rules page, follow-ups), plus
   Postgres in CI.
3. Tailscale between the PC and the data box (a data-vault PR for the box side).
4. The PC worker on Postgres: today's `process.py` steps, with the syncing removed.
5. Streamlit on Postgres (rewrite vs `ATTACH`, §6).
6. Switch over (§8).
7. bank-enrichment's bronze config in data-vault, then the test restore from B2.

## 10. Open questions

- **Worker schedule:** keep Task Scheduler (daily at a set time, plus whenever you
  run it by hand), or every 15 minutes while the PC is on?
- **Streamlit's queries:** rewrite or `ATTACH` (§6).
- **The pre-change backup in Settings:** keep it as a schema `pg_dump`, or rely on the nightly backups?
- **Merge the two dashboards later?** Not needed now.
