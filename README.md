# Bank Enrichment

## What Is This?

Most financial tools answer one question: *what category was this transaction?*

This project answers a different question: *what did this transaction actually mean?*

Bank Enrichment is a personal financial intelligence pipeline that captures 
real-time transaction context at the moment of purchase — while it's still 
fresh — and builds a structured, enriched dataset that can be analysed, 
queried, and reprocessed over time.

The core insight is that a transaction like `TESCO £43.20` is almost meaningless 
in isolation. But `weekly shop, bought ingredients for a dinner party, plus 
impulse snacks` is a datapoint you can actually reason about — now and in ten 
years time.

## Why Context at the Point of Purchase?

Traditional budgeting tools ask you to categorise transactions at the end of 
the month. By then, you've forgotten why you spent £12 at a service station or 
what that random Amazon charge was.

This system sends you a Telegram notification the moment a transaction happens 
and asks for one sentence of context. That's it. Thirty seconds while the memory 
is still there.

## Why Store Context Separately From Labels?

Labels change. The categories you care about today won't be the same in five 
years. By storing your raw context separately from any classification layer, 
you can re-run labelling at any point using a new taxonomy — without losing 
any of your original data.

Feed your enriched transaction history to an LLM with a completely different 
set of categories and it will reclassify everything correctly, because the 
context sentences tell it exactly what each transaction was.

## How It Works

1. Monzo transaction fires a webhook
2. Always-on server receives and stores the raw payload
3. Server checks your rules — if a rule matches, the transaction is auto-enriched and Telegram is skipped entirely
4. Otherwise, a Telegram notification is sent to your phone with transaction details
5. You reply with one sentence of context — or tap Skip to dismiss
6. Enrichment stored alongside the raw transaction in the queue
7. If you don't respond, the system follows up at 1 hour, 1 day, 2 days, and 1 week — then auto-skips
8. Daily local script pulls enriched transactions from the server into a local DuckDB database
9. LLM classifier (Claude) assigns each transaction a parent category and subcategory using a living taxonomy it builds and refines over time
10. Local Streamlit dashboard lets you explore your spending, view charts, and correct labels

## Server Dashboard

The server exposes a password-protected dashboard at `https://your-name.duckdns.org/dashboard`.

**Lifetime Stats** — persistent counters that survive queue clears:
- Total transactions received and total monetary value processed
- Total Telegram notifications sent, enriched, and processed

**Current Queue** — live state of unprocessed transactions:
- Status breakdown (pending / enriched / skipped) with counts
- Paginated transaction list with amount and status
- Enrich any pending transaction directly from the dashboard via a modal
- Skip or delete transactions inline

Each transaction links to a detail page showing the full payload, 
merchant/counterparty info, enrichment context, and controls to enrich, 
skip, reset, or delete.

A **Rules view** at `/dashboard/rules` lets you define auto-enrichment rules. 
When a transaction matches a rule, it is enriched automatically and no Telegram 
notification is sent — useful for recurring transactions like rent, gym memberships, 
or regular transfers where you already know the context.

A **Database view** at `/dashboard/db` lets you inspect the raw tables directly 
without needing to exec into the container.

## Rules

Rules are matched against each incoming transaction before the Telegram notification fires. 
If a rule matches, the transaction is auto-enriched with the rule's context and the 
notification is skipped entirely.

Each rule specifies:

| Field | Description |
|---|---|
| Name | A label for the rule (e.g. "Gym membership") |
| Match field | What to check — merchant name, description, counterparty name, category, or amount |
| Match type | How to compare — `contains`, `exact`, `regex`, or `amount_range` |
| Match value | The value to match against (e.g. `PureGym`, or `500-600` for an amount range in £) |
| Auto context | The context sentence to store (e.g. "Monthly gym membership") |

Rules can be enabled or disabled at any time from the dashboard. Amount ranges are specified 
in pounds (e.g. `490-510`) and matched against the absolute transaction value.

## LLM Classification

A two-pass classification system runs locally against your DuckDB database:

**Pass 1 — Parent category:** Claude assigns each transaction to a broad category 
(e.g. Holidays & Travel, Eating Out, Food & Groceries). If a transaction's context 
mentions a holiday, it is always grouped under Holidays & Travel regardless of what 
was purchased — so all your holiday spending stays together.

**Pass 2 — Subcategory:** Within each parent, Claude assigns a specific subcategory 
(e.g. Accommodation, Car Rental, Holiday Food).

The taxonomy starts empty and grows over time. Claude reuses existing categories 
wherever they fit and only creates new ones when genuinely needed. Because context 
is stored separately from labels, you can wipe the taxonomy and re-run classification 
at any point — with the same categories or entirely new ones.

## Local Dashboard

A Streamlit dashboard runs on your machine and reads directly from the local DuckDB database.

| Tab | Description |
|-----|-------------|
| Overview | KPI cards (spend, income, net, unclassified) + spend and income charts by category |
| Spending Over Time | Stacked monthly spend chart + monthly spend/income/net table |
| Transactions | Full filterable and searchable table — edit labels inline, type new ones |
| Category Drill-Down | Pick any parent category to see subcategory breakdowns and transactions |
| Taxonomy | View, rename, and add parent categories and subcategories |

## What You End Up With

A local DuckDB database of every transaction, each row containing:

- The raw bank data (amount, merchant, timestamp, counterparty, full JSON payload)
- Your one-sentence human context (what it actually was)
- LLM-assigned parent category and subcategory
- Status tracking (enriched / skipped / auto-skipped)
- Full audit trail (received, enriched, processed, classified timestamps)

Because context is stored separately from classification, you can re-run labelling 
at any point using a new taxonomy and it will classify correctly every time.

## Project Structure

```
├── src/
│   ├── server_scripts/        # FastAPI server (runs in Docker)
│   │   ├── main.py            # API endpoints, Telegram callbacks, dashboard
│   │   ├── check_rules.py     # Rule matching logic
│   │   ├── telegram.py        # Telegram bot logic
│   │   ├── follow_up_tg.py    # Follow-up notification scheduler
│   │   └── server_db.py       # Server-side database functions
│   └── local_scripts/         # Runs on your local machine
│       ├── process.py         # Pull enriched transactions from server
│       ├── llm_labelling.py   # LLM classification (Claude)
│       ├── database_functions.py  # Shared DuckDB library
│       ├── dashboard.py       # Streamlit dashboard
│       ├── view_db.py         # Print all transactions to terminal
│       ├── clear_db.py        # Wipe transaction database
│       └── clear_taxonomy.py  # Wipe category tables
├── sql/
│   ├── tables.sql             # Local database schema (transactions, categories)
│   └── server_tables.sql      # Server database schema (queue, stats, rules)
├── config/
│   └── .env.example           # Environment variable template
├── Dockerfile                 # Server container
├── docker-compose.yml         # Server + Caddy
└── Caddyfile                  # Reverse proxy + automatic HTTPS
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Server | Python, FastAPI, DuckDB |
| Reverse proxy | Caddy (automatic HTTPS) |
| Notifications | Telegram Bot API |
| Local database | DuckDB |
| LLM classification | Anthropic Claude (Sonnet) |
| Local dashboard | Streamlit, Plotly |
| Deployment | Docker Compose |

## Setup

See [SETUP.md](SETUP.md) for full setup instructions.
