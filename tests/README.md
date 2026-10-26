# Tests - To be run in CI workflow

Run all tests:

```
poetry run pytest
```

Run a specific file:

```
poetry run pytest tests/test_database_functions.py
```

Run with verbose output:

```
poetry run pytest -v
```

With coverage, as CI runs it (it fails below 80%):

```
poetry run pytest --cov=src --cov-report=term-missing:skip-covered --cov-fail-under=80
```

Waits between retries (Telegram, Monzo's API, the worker) never really
sleep in a test: the `retry_waits` fixture records them instead.

## The database tests need a Postgres

Most tests use a throwaway Postgres schema, built from `sql/postgres/schema.sql`
and dropped afterwards. Point `TEST_POSTGRES_URL` at an empty Postgres -- never
the real database -- or those tests are skipped. CI sets it, against a Postgres
it starts empty for each run (and sets `REQUIRE_POSTGRES_TESTS`, so a missing
one fails the run instead of quietly skipping). Locally:

```
docker run --rm -e POSTGRES_PASSWORD=x -p 5432:5432 postgres:18
TEST_POSTGRES_URL=postgresql://postgres:x@localhost:5432/postgres poetry run pytest
```

## Structure

| File | What it tests |
|---|---|
| `test_characterization.py` | Everything the pipeline does, against a recorded result (`golden/characterization.json`) -- see below |
| `test_database_functions.py` | Reading and writing transactions, labels, categories, roles, subscriptions, backups |
| `test_llm_labelling.py` | The classifier's prompts and passes (Anthropic client mocked) |
| `test_placement_judge.py`, `test_judge_backlog.py` | The second-look review of placements |
| `test_category_proposals.py`, `test_taxonomy_review.py`, `test_rule_suggestions.py` | Proposals, the monthly review, rule suggestions |
| `test_*_flow.py` | Round trips: the pipeline writes a proposal, the server sends its card, a tap is recorded, the next run acts on it |
| `test_worker.py` | The hourly runs on the server: their schedule, alerts, one run at a time |
| `test_summaries.py` | Weekly and monthly summaries |
| `test_dashboard_apptest.py`, `test_dashboard_helpers.py` | The Streamlit dashboard |
| `test_postgres_schema.py` | The tables and views themselves |
| The rest | The server: webhooks, Telegram, rules, follow-ups, its dashboard, security |

## Fixtures

`conftest.py` provides:

- **`pg`** -- the throwaway Postgres schema, as a psycopg connection.
- **`db`** -- the pipeline's own connection to that same schema, set as the one
  `database_functions` hands out, so every pipeline function works on it.
- **`server_con`** -- the same, for the server's `server_db`.
- **`_reset_db`**, **`alert_post`**, **`_backups_in_tmp`**, **`_no_real_monzo_tokens`**
  (all automatic) -- keep each test isolated, and keep Telegram, backups and
  Monzo away from anything real.

`tests/local_helpers.py` and `tests/server_helpers.py` build transactions and
categories the way they really arrive.

Tests that don't touch the database (pure functions, mocked HTTP calls) need no
fixtures.

## The characterization test

`test_characterization.py` runs one realistic dataset, with a fixed clock,
through every read function and a fixed sequence of what the pipeline does, and
compares every result and every table with `golden/characterization.json`. It
was recorded before the pipeline moved off DuckDB, and proves that move changed
nothing. Any change to what a function returns or writes fails it.

After a deliberate change, re-record it and review the JSON diff like any other:

```
UPDATE_GOLDEN=1 poetry run pytest tests/test_characterization.py
```

`CHAR_DUMP=<file>` writes a run's results to a file, for diffing when it fails.
