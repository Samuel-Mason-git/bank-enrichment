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
5. You reply with one sentence of context, tap a quick-category button if one's shown, or tap Skip to dismiss
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

A **Logout** button sits in the header. HTTP Basic Auth has no real server-side 
session for it to end, so it links to a page explaining this plainly rather than 
attempting a fake login challenge — closing the browser tab is the only way to 
actually clear cached credentials.

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

## Quick Categories

Typing a sentence for every transaction adds up. Each time the local pipeline runs, 
it computes your most-used subcategories overall and per-merchant, and syncs a small 
summary (category names only — no amounts, dates, or raw transaction data) to the server. 
The next Telegram notification for a matching merchant shows up to 3 one-tap buttons for 
that merchant's usual categories, or 5 general ones if the merchant hasn't built up enough 
history yet, alongside the usual Enrich and Skip buttons. Tapping one instantly saves that 
category as your context — no typing required, and the LLM classifier still runs afterwards 
as normal so it stays part of the same taxonomy.

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

This is a starting point, not a constraint. Everything can be changed from the **Settings tab** 
in the local dashboard — rename parents, add or delete subcategories, move subcategories 
between parents, view which transactions sit under each label, and wipe labels per category 
so the LLM re-classifies against your updated structure. Because context is stored separately 
from labels, restructuring the taxonomy and re-running classification never loses any data.

The default taxonomy is only seeded into a genuinely empty database — the very first time
you run the pipeline or dashboard. Once any parent category exists, seeding is skipped
entirely, so wiping the taxonomy from the Settings tab and building your own from scratch
is safe: the defaults won't quietly get added back in alongside it on a later restart.

### Category Roles & Standard vs Actual

Every category has a **Role** — Income, Spend, Investment, Transfer, or Excluded — set from
the Settings tab, at the parent level (subcategories inherit it, or can override it
individually, e.g. a "Refunds" subcategory under "Income" is Excluded rather than Income).
Roles drive what actually counts as income or spend in the weekly/monthly Telegram summaries
and throughout the dashboard, so a refund or an internal transfer between your own accounts
doesn't inflate your real income or spend.

This is why most of the dashboard shows two versions of the same figures:

- **Standard** — every £ counted by amount sign alone (money in = income, money out = spend),
  regardless of category.
- **Actual** — the same figures computed from each category's Role instead, so refunds,
  transfers, and investment movements are correctly excluded or reclassified.

## Local Dashboard

A Streamlit dashboard runs on your machine and reads directly from the local DuckDB database.
Every tab respects a shared sidebar: a quick date-range preset or custom range, parent/subcategory
filters, a text search across merchant/description/context, toggles for skipped/unclassified
transactions, and an **Exclude** block for hiding noise — pick merchants/counterparties from a
dropdown, or paste transaction IDs for one-offs that have no name to match on. Exclusions apply to
the current period, the previous period it's compared against, and the CSV export, so every figure
stays consistent. They last for the browser session only and are never written to the database —
excluding something hides it from view, it does not delete or reclassify it.

| Tab | Description |
|-----|-------------|
| Overview | Standard vs Actual metric cards (Income, Spend, Net, Savings Rate, Invested, Transferred/Excluded), each with a colored delta badge vs the previous period (direction-aware — e.g. a rise in Spend reads as unfavorable, a rise in Income as favorable). Outgoings/Incomings breakdown charts with a metric selector and Category/Subcategory toggle. |
| Over Time | Average-per-period stat cards; a Savings & Investing chart (Savings Rate %, Investment Rate %, and Actual Savings £ together); Spending Over Time / Income Over Time sections with per-category and per-subcategory monthly trend lines (each with a Total line and a 3-month rolling average overlaid); a Monthly Totals table (collapsed by default) with a tooltip on every column header. |
| Transactions | Full filterable table with a few "fun fact" stat cards up top (unique merchants, biggest transaction, busiest day, etc). Category and Subcategory are edited via dropdowns restricted to your existing taxonomy, so labels stay consistent; Date and Context are also editable; Merchant and Description are read-only. |
| Category Drill-Down | Pick any parent category to see role-based metric cards (Income/Spend/Invested/Transferred-Excluded, split into separate Outgoing/Incoming cards whenever a category has both), subcategory breakdown charts, and a Subcategory Summary table with average transaction size, last-seen date, and % of category. |
| Top Merchants | Ranks merchants by Total Spend or Frequency (toggle), with search, a Top-N selector, and stat cards (merchant count, total spend, average per merchant, top merchant). Matches on merchant name or counterparty name, so direct debits and bank transfers that never populate a merchant name are still included. |
| Subscriptions | Auto-detected recurring payments — matches by merchant name *or* counterparty name (many direct debits only populate the latter), requires more supporting occurrences before suggesting a fast-cadence (weekly/fortnightly) pattern, and ignores patterns whose last occurrence is stale relative to their cadence. Confirmed subscriptions auto-deactivate if no matching transaction has landed in the last 2 months. |
| Settings | Stat cards (parent categories, subcategories, % classified). Tree view of all categories with a Role selector per parent (and per-subcategory override) driving the Standard vs Actual figures elsewhere. Add, rename, move, and delete categories — renaming keeps a category's custom Role, and a clear error is shown if a rename would collide with an existing category name. Click any subcategory to view its transactions or bulk-reassign them. Wipe labels per category to force re-classification, or wipe the entire taxonomy structure from the Danger Zone at the bottom (heavily guarded — requires typing a confirmation phrase). Also holds the Backups section (see below). |

### Backups

A full backup of the local database is taken automatically before anything irreversible —
deleting a category or subcategory, wiping labels, bulk-reassigning transactions to another
subcategory, or wiping the entire taxonomy. There is nothing to enable; the Backups section in
the Settings tab also has a **Back up now** button for taking one by hand.

Backups are written to a `backups/` folder next to your database, one directory per backup, named
`<timestamp>_<reason>`. Each is a DuckDB `EXPORT DATABASE` — a logical dump (`schema.sql`,
`load.sql`, and one Parquet file per table) rather than a copy of the `.db` file, which is open
and may have un-checkpointed changes sitting in its WAL. Restore one with:

```sql
IMPORT DATABASE 'path/to/backups/20260813-220341_manual';
```

Every backup carries a `manifest.json` recording what it captured at the time it was taken, so the
Settings tab can tell you what you'd get back before you restore: transaction count, how many were
classified, taxonomy size, subscription count, the date range covered, and size on disk. The most
recent 10 are kept and older ones pruned automatically; any backup can also be deleted by hand
from the same section (behind a confirmation, since it's the safety net for everything else).

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
│       ├── database_functions.py  # Shared DuckDB library — includes a CLI (run the file directly) to print stats/transactions
│       ├── dashboard.py       # Streamlit dashboard
│       └── dashboard_helpers.py   # Pure logic used by the dashboard (role/subscription/chart helpers) — unit tested standalone
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
| CI/CD | GitHub Actions — tests run on every PR; merging to `main` auto-deploys to the server over SSH |

## Setup

See [SETUP.md](SETUP.md) for full setup instructions.
