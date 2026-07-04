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

Tests that don't touch the database (pure functions, mocked HTTP calls) need no fixtures.
