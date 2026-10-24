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

## Structure

| File | What it tests |
|---|---|
| `test_database_functions.py` | DuckDB CRUD — transactions, categories, subscriptions, search, stats |
| `test_llm_labelling.py` | Prompt builders, JSON extraction, LLM call wrappers (Anthropic client mocked) |
| `test_summaries.py` | Week/month date helpers, message formatting, missing-period detection |
| `test_process.py` | `fetch_enriched` and `mark_processed` HTTP calls (requests mocked) |
| `test_check_rules.py` | Rule matching logic — exact, contains, regex, amount range/exact |

## How fixtures work

`conftest.py` provides two fixtures:

- **`db`** — an in-memory DuckDB connection with the full schema applied. Inject it into any test that needs a database. The connection is set on the `database_functions._con` global so all DB functions work without touching the real DB file.
- **`_reset_db`** (autouse) — resets `database_functions._con` to `None` before and after every test, keeping state isolated.
- **`pg`** — a throwaway Postgres schema with today's tables (`sql/postgres/schema.sql`), dropped after the test. It needs `TEST_POSTGRES_URL`. CI sets it, pointing at an empty Postgres it starts for each run. Without it, these tests are skipped, so the suite still runs on a machine with no Postgres. To run them locally, start a scratch Postgres (`docker run --rm -e POSTGRES_PASSWORD=x -p 5432:5432 postgres:18`) and set `TEST_POSTGRES_URL=postgresql://postgres:x@localhost:5432/postgres`.

Tests that don't touch the database (pure functions, mocked HTTP calls) need no fixtures.
