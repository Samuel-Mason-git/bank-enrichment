# Contributing

Thanks for looking. Bank Enrichment is one person's setup, shared as a working example. Bug reports, documentation fixes and pull requests are all welcome.

## Before you start

For anything bigger than a small fix, open an issue first so we can agree on the approach.

## Setup

The project uses Python 3.11 or newer and [Poetry](https://python-poetry.org/). [SETUP.md](SETUP.md) walks through the full setup. On Windows, run Python tooling inside WSL: Windows Smart App Control blocks pandas' compiled DLLs when run directly.

```bash
poetry install
```

## Tests

```bash
poetry run pytest
```

The Postgres tests need an empty Postgres to build throwaway schemas in. Point `TEST_POSTGRES_URL` at one. Without it they're skipped, and `REQUIRE_POSTGRES_TESTS=1` makes a missing URL fail instead:

```bash
docker run -d --rm --name bank-enrichment-test -p 5432:5432 -e POSTGRES_PASSWORD=ci postgres:18
TEST_POSTGRES_URL=postgresql://postgres:ci@localhost:5432/postgres REQUIRE_POSTGRES_TESTS=1 poetry run pytest
```

Coverage must stay at 80% or more. CI enforces it with `--cov-fail-under=80`.

## Pull requests

- Branch from `main`, and keep each PR to one change.
- Say what changed, why, and how you tested it. The PR template asks for this.
- CI must pass: the tests, the coverage floor, and the server image build.
- If behaviour changes, update the matching page in `docs/` and, where it applies, `SETUP.md`.
- Use made-up data in tests, screenshots, issues and logs. Don't include real transactions, account details or API keys.

## Commit messages

A short imperative subject. The body says why the change is needed.
