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
3. Server checks your rules — if a rule matches, the transaction is auto-enriched and/or marked as skipped, and Telegram is skipped entirely
4. Otherwise, a Telegram notification is sent to your phone with transaction details
5. You reply with one sentence of context — or tap Skip to dismiss
6. Enrichment stored alongside the raw transaction in the queue
7. If you don't respond, the system follows up at 1 hour, 1 day, 2 days, and 1 week — then auto-skips
8. Daily local script pulls enriched transactions from the server into a local DuckDB database, then immediately runs the LLM classifier in the same process — one scheduled task does everything. Processed transactions remain on the server for 5 days to catch delayed settlement webhooks, then are cleaned up automatically
9. LLM classifier (Claude) assigns each transaction a parent category and subcategory using a living taxonomy it builds and refines over time
10. Every Monday, a weekly summary is sent to your Telegram — spend, income, net, and a breakdown by category for the week just ended. A monthly summary follows at the end of each month.
11. Local Streamlit dashboard lets you explore your spending, view charts, correct labels, and track subscriptions

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

Processed transactions are retained on the server for 5 days before being automatically 
cleaned up. This acts as a deduplication buffer — some merchants (e.g. Aldi, Lidl) do 
not send a pending webhook and only fire when the transaction settles, which can be days 
after the original purchase. Without this buffer, the settlement webhook would be treated 
as a new transaction.

## Rules

Rules are matched against each incoming transaction before the Telegram notification fires. 
If a rule matches, the transaction is auto-enriched with the rule's context and the 
notification is skipped entirely.

Each rule specifies:

| Field | Description |
|---|---|
| Name | A label for the rule (e.g. "Wifi Bill") |
| Match field | What to check — merchant name, description, counterparty name, category, or amount |
| Match type | How to compare — `contains`, `exact`, `regex`, `amount_range`, or `amount_exact` |
| Match value | The value to match against (e.g. `EE`, `490-510` for a £ range, or `9.99` for exact) |
| Auto context | The context sentence to store (e.g. "Monthly wifi bill") — optional if Auto Skip is on |
| Auto Skip | If checked, the transaction is marked as skipped automatically — no Telegram notification, no manual enrichment needed |
| Second condition | Optional — a second match field/type/value that must also pass (AND logic) |

Rules can be enabled or disabled at any time from the dashboard without deleting them.
Amount ranges and exact amounts are specified in pounds and matched against the absolute
transaction value. A rule with two conditions only fires if both match — useful for
cases like a specific merchant at a specific amount.

**Auto Skip** is useful for transactions you never want to see — internal transfers, 
refunds from known merchants, or any recurring charge you're confident needs no context. 
The transaction is still stored and will appear in your local database; it just won't 
interrupt you via Telegram. You can combine Auto Skip with an Auto Context if you want 
the transaction labelled but not notified.

## Weekly & Monthly Summaries

The daily processing script automatically sends two types of Telegram summary.

**Weekly summary** — sent every Monday for the week just ended (Mon–Sun):

- Total spend and income (skipped transactions excluded)
- Net for the week
- Spend and income broken down by parent category
- Any unclassified amount shown as a separate line so the totals always add up

**Monthly summary** — sent once the month ends, the next time the script runs:

- Total spend and income
- Net (saved or overspent)
- Full breakdown by parent category for both spend and income

Both summaries catch up automatically if the script was inactive — a summary is sent for each missed period in order, and each is recorded locally so it is never sent twice.

## LLM Classification

A three-pass classification system runs locally against your DuckDB database:

**Pass 0 — Match existing taxonomy:** Claude first checks whether each transaction 
confidently matches an already-existing subcategory. If it does, the transaction is 
classified immediately and Passes 1 and 2 are skipped. This keeps the taxonomy 
consistent over time — a transaction classified as "Tobacco & Nicotine" once will 
always land there on future runs.

**Pass 1 — Parent category:** For transactions that didn't match in Pass 0, Claude 
assigns a broad parent category. It thinks carefully about overlapping categories 
and picks the most accurate fit — for example, "Holidays" is only used when the 
context explicitly mentions a holiday trip, not just any travel spend.

**Pass 2 — Subcategory:** Within each parent, Claude assigns a specific subcategory, 
creating new ones only when none of the existing ones are an accurate fit.

A default taxonomy is seeded on first run — 13 parent categories and ~70 subcategories 
covering most common personal spending:

| Parent | Example subcategories |
|--------|----------------------|
| Bills & Utilities | Rent, Electricity, Internet, Mobile Phone |
| Food & Drink | Groceries, Restaurants, Takeaway, Coffee Shops |
| Transport | Public Transport, Fuel, Taxi & Rideshare, Parking |
| Shopping | Clothing, Electronics, Gifts, Home Goods |
| Holidays | Accommodation, Holiday Food, Local Transport |
| Entertainment | Streaming, Cinema, Events, Video Games |
| Health | Pharmacy & Medication, Dental, GP / Medical |
| Income | Salary, Refunds, Interest, Dividends |
| Investments | Savings Deposits, Stocks & Shares ISA, Pension |
| Subscriptions & Software | AI Tools, Cloud Storage, Streaming |
| Personal Care & Consumables | Haircuts, Toiletries, Tobacco & Nicotine |
| Career & Learning | Online Learning, Books, Certifications |
| Transfers | Inbound Transfer, Outbound Transfer |

This is a starting point, not a constraint. Everything can be changed from the **Taxonomy tab** 
in the local dashboard — rename parents, add or delete subcategories, move subcategories 
between parents, view which transactions sit under each label, and wipe labels per category 
so the LLM re-classifies against your updated structure. Because context is stored separately 
from labels, restructuring the taxonomy and re-running classification never loses any data.

## Local Dashboard

A Streamlit dashboard runs on your machine and reads directly from the local DuckDB database.

| Tab | Description |
|-----|-------------|
| Overview | KPI cards (spend, income, net, unclassified) + spend and income charts by category |
| Spending Over Time | Stacked monthly spend chart + monthly spend/income/net/transaction count table |
| Transactions | Full filterable and searchable table — edit labels inline, type new ones |
| Category Drill-Down | Pick any parent category to see subcategory breakdowns and transactions |
| Subscriptions | Auto-detected recurring payments + manual add, active/inactive toggle, monthly and annual cost totals |
| Taxonomy | Tree view of all categories — add, rename, move, and delete. Click any subcategory to view its transactions or bulk-reassign them. Wipe labels per category to force re-classification. |

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
│       ├── process.py         # Pull, classify, and send summaries — one task does all
│       ├── llm_labelling.py   # LLM classification (Claude)
│       ├── monthly_summary.py # Monthly Telegram summary (spend, income, categories)
│       ├── weekly_summary.py  # Weekly Telegram summary (spend, income, categories)
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
