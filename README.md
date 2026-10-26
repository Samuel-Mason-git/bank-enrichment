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
2. Always-on server stores the raw payload in the Postgres database (on a separate data box, backed up nightly)
3. Server checks your rules — if a rule matches, the transaction is auto-enriched and/or marked as skipped, and Telegram is skipped entirely
4. Otherwise, a Telegram notification is sent to your phone with transaction details
5. You reply with one sentence of context, tap a quick-category button if one's shown, or tap Skip to dismiss
6. Your context is stored as an event alongside the raw transaction — every answer and every label is kept, so nothing is ever overwritten
7. If you don't respond, the system follows up at 1 hour, 1 day, 2 days, and 1 week — then auto-skips
8. Every morning at 08:00 a worker on the server runs the LLM classifier and the rest of the pipeline against the same database — nothing needs your PC to be on
9. LLM classifier (Claude) assigns each transaction a parent category and subcategory using a living taxonomy it builds and refines over time — a genuinely new category is never created silently; it's proposed with a Telegram approval card first (see [New Category Approval](#new-category-approval) below)
10. Every Monday, a weekly summary is sent to your Telegram — spend, income, net, and a breakdown by category for the week just ended. A monthly summary follows at the end of each month. Once a month, the pipeline also reviews your existing taxonomy for subcategories that have quietly grown a coherent cluster of mislabelled transactions inside them, and proposes a split or move (see [Monthly Taxonomy Review](#monthly-taxonomy-review) below).
11. Local Streamlit dashboard lets you explore your spending, view charts, correct labels, and track subscriptions

## Server Dashboard

The server exposes a password-protected dashboard at `https://your-name.duckdns.org/dashboard`.

**Lifetime Stats** — counted from the transactions themselves:
- Total transactions received and total monetary value
- Telegram requests sent, and how many were enriched and classified

**Current Queue** — where each transaction is up to:
- Status breakdown (pending / enriched / classified / skipped) with counts
- Paginated transaction list with amount and status
- Enrich any pending transaction directly from the dashboard via a modal
- Skip, unskip or delete transactions inline

Each transaction links to a detail page showing the full payload,
merchant/counterparty info, enrichment context, and controls to enrich,
skip, unskip, or delete.

A **Rules view** at `/dashboard/rules` lets you define auto-enrichment rules. 
When a transaction matches a rule, it is enriched automatically and no Telegram 
notification is sent — useful for recurring transactions like rent, gym memberships, 
or regular transfers where you already know the context.

A **Database view** at `/dashboard/db` lets you inspect the raw tables directly
without needing to connect to the database.

A **Logout** button sits in the header. HTTP Basic Auth has no real server-side 
session for it to end, so it links to a page explaining this plainly rather than 
attempting a fake login challenge — closing the browser tab is the only way to 
actually clear cached credentials.

**Who can reach what.** The dashboard needs your login. The two webhooks can't use
one — Telegram and Monzo have to be able to call them — so each checks a shared secret instead: Telegram
sends `TELEGRAM_WEBHOOK_SECRET` in a header, and Monzo's registered webhook URL carries
`MONZO_WEBHOOK_SECRET` (redacted from the request logs). On top of that, the bot only
acts on updates from your own `TELEGRAM_CHAT_ID`, so someone who finds it can't use it.
Because browsers attach a saved Basic-auth login to *any* form submission, even one
another website triggers, dashboard changes are refused unless they come from the
dashboard's own pages. Monzo authorization starts from `/oauth/start` behind your login,
and the callback only completes an authorization the server issued itself.

Every transaction is kept. A webhook for a transaction already stored is ignored — some
merchants (e.g. Aldi, Lidl) only fire when the transaction settles, days after the purchase,
and that must not become a second transaction.

## Rules

Rules are matched against each incoming transaction before the Telegram notification fires. 
If a rule matches, the transaction is auto-enriched with the rule's context and the 
notification is skipped entirely.

Each rule specifies:

| Field | Description |
|---|---|
| Name | A label for the rule (e.g. "Wifi Bill") |
| Match field | What to check — merchant name, description, counterparty name, category, payment scheme, or amount |
| Match type | How to compare — `contains`, `exact`, `regex`, `amount_range`, `amount_exact`, or (amount field only) `amount_negative`/`amount_positive` for direction alone |
| Match value | The value to match against (e.g. `EE`, `490-510` for a £ range, `9.99` for exact) — not needed for `amount_negative`/`amount_positive`, which only look at the sign |
| Auto context | The context sentence to store (e.g. "Monthly wifi bill") — optional if Auto Skip is on or a Fixed Category is set |
| Fixed category | Optional — a category and subcategory picked from your own taxonomy. A match is filed straight there, and the classifier never runs on it |
| Auto Skip | If checked, the transaction is marked as skipped automatically — no Telegram notification, no manual enrichment needed |
| Second condition | Optional — a second match field/type/value that must also pass (AND logic) |

Rules can be enabled or disabled at any time from the dashboard without deleting them.
Amount ranges and exact amounts are specified in pounds and matched against the absolute
transaction value, regardless of direction — `amount_negative`/`amount_positive` exist
specifically for when direction itself is what you need to match on (see the Savings
example below). A rule with two conditions only fires if both match — useful for cases
like a specific merchant at a specific amount, or a payment scheme in one direction only.

**Payment scheme** is Monzo's own classification of how money moved (e.g. `uk_retail_pot`
for a transfer to or from a pot) — matching on it is exact by nature, unlike a text match
on the description, which for a pot transfer is just the pot's internal ID and can
coincidentally contain another merchant's name as a substring.

**Auto Skip** is useful for transactions you never want to see — internal transfers, 
refunds from known merchants, or any recurring charge you're confident needs no context. 
The transaction is still stored and will appear in your local database; it just won't 
interrupt you via Telegram. You can combine Auto Skip with an Auto Context if you want 
the transaction labelled but not notified.

**Auto Context vs Fixed Category.** An Auto Context rule only supplies the context
sentence — the transaction still goes through quick-tap or Claude to be placed, which
costs a classification call and can, in principle, land somewhere different from last
time. A **Fixed Category** rule skips all of that: the server files the match straight
into the chosen category as it arrives (logged as source `rule`) and the classifier never
sees it. Use it when the answer is never in doubt — a phone bill, a subscription, rent.
The dropdowns list your real categories, so a typo can't quietly create a near-duplicate
category. The rule points at the category itself, so renaming it carries the rule along;
if it's deleted, matches are left for the classifier instead. Editing or resetting a transaction's context by hand
clears any rule placement on it, so what you typed always wins.

**A card payment's amount isn't always final.** A provisional authorisation hold (the
classic UK fuel-pump pattern — a flat ~£99/£100 hold before the pump knows how much fuel
was actually taken) is detected automatically and auto-skipped, the same way a declined
payment is: no Telegram card, since the amount was never going to be the real charge. The
real, final amount still arrives normally, as its own separate transaction.

**Worked example — tracking a Monzo savings pot separately from real investing:** two
rules, both matching **Payment scheme** `exact` `uk_retail_pot`, with a second condition
of **Amount** `amount_negative` on one (Auto Context "Moved into savings pot") and
`amount_positive` on the other ("Moved out of savings pot"). Once a few transactions have
gone through each, set that subcategory's Role to **Saving** in the Settings tab — see
[Category Roles & Standard vs Actual](#category-roles--standard-vs-actual).

### Creating a Rule From an Existing Transaction

Picking a transaction from the dropdown above the Add Rule form pre-fills it for you —
merchant name if present, else counterparty name, else description (the same fallback
order used throughout), plus that transaction's existing context if it has one.
**Test this rule** runs the not-yet-saved rule against the last 30 days of transactions
and lists exactly which ones it would catch, with their status and current context, right under the form — so you see its
real reach before saving, not just that it matches the transaction you built it from.
The form stays filled in afterwards.

### Rule Suggestions

Each pipeline run also scans your already-classified transactions for a merchant or
counterparty that has shown up 5+ times with the *exact same* category/subcategory every
single time — one disagreement anywhere in its history disqualifies it, since the whole
point is a pattern reliable enough to automate.

Statistical consistency in your own history isn't the same as a merchant being safe to
automate, though — a supermarket that's happened to be Groceries every time so far could
just as easily be something else next time, while a single-purpose merchant (a phone
bill, a subscription) realistically never will be. Every qualifying pattern is checked by
Claude for exactly this before anything is suggested — one small batched call per run, 
only when there's a pattern it hasn't asked about before — and anything judged a general-purpose
retailer/marketplace is dropped rather than suggested. (Fails closed: if that check
itself fails, nothing is suggested that run rather than risk suggesting something
unverified — it's simply reconsidered next time.)

A pattern that survives both checks, and isn't already covered by an existing enabled
rule, gets a Telegram card of its own: **💡 Rule suggestion**, with the suggested context
pre-filled from whichever context sentence you've typed most often for that merchant
(falling back to a generic "`<merchant>` purchase" if your wording has never settled on
one) and Claude's own reasoning for why it's safe to automate. Tapping **Create rule**
adds it immediately as a **Fixed Category** rule — future matches go straight into that
category without a classification call, which is safe precisely because the pattern has
never once varied and was judged single-purpose first. Tapping **No thanks** means that
exact pattern is never suggested again.

## Quick Categories

Typing a sentence for every transaction adds up. The server works out your most-used
subcategories overall and per-merchant from your labelled transactions, so the next
Telegram notification for a matching merchant shows up to 3 one-tap buttons for 
that merchant's usual categories, or 5 general ones if the merchant hasn't built up enough 
history yet, alongside the usual Enrich and Skip buttons. Tapping one instantly saves that
category as your context — no typing required, and the next pipeline run files it there
(after the second look below). Each button names its category by id, so a card tapped
days later still files exactly what it said, even if the buttons have changed since.

The per-merchant personalization depends on Monzo actually sending a `merchant` name on new
transactions, which in turn depends on Monzo's own access confirmation staying current — see
"Set Up the Monzo OAuth Confidential Client" in SETUP.md. If that lapses, categorization itself
keeps working fine from the raw description text; only the personalized quick-pick buttons fall
back to a generic top-5 list instead of that merchant's own history.

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

Both summaries catch up automatically if the script was inactive — a summary is sent for each missed period in order, and each is recorded in the database so it is never sent twice.

## LLM Classification

A multi-pass classification system runs every morning on the server, against the shared database:

**Pass 0 — Match existing taxonomy:** Claude first checks whether each transaction 
confidently matches an already-existing subcategory. If it does, the transaction is 
classified immediately and the remaining passes are skipped. This keeps the taxonomy 
consistent over time — a transaction classified as "Tobacco & Nicotine" once will 
always land there on future runs.

**Pass 1 — Parent category:** For transactions that didn't match in Pass 0, Claude 
assigns a broad parent category. It thinks carefully about overlapping categories 
and picks the most accurate fit — for example, "Holidays" is only used when the 
context explicitly mentions a holiday trip, not just any travel spend. A transaction 
with a genuinely distinct, one-off purpose (a payment to a tax authority, a one-time 
legal or medical event) is treated as deserving its own new parent, not squeezed into 
a loosely-related existing one just because it seems adjacent.

**Pass 2 — Subcategory:** Within each parent, Claude assigns a specific subcategory, 
creating new ones only when none of the existing ones are an accurate fit.

**Pass 3 — Alternatives:** Only for transactions that didn't cleanly match anything 
existing, Claude is asked for up to 2 more genuinely distinct placements alongside 
its first choice — a stretch-fit into an existing category it wasn't confident enough 
to auto-match, or a different new-category idea — each with a one-line rationale. See 
[New Category Approval](#new-category-approval) for what happens with these.

Passes 0 and 2 also see how big and how regular the transactions already in each 
subcategory are (amount range and typical gap between them), so a one-off £282 
deposit isn't matched into a subcategory of £1,315 monthly rent just because the 
topics sound related. See [Second-Look Review](#second-look-review) for the check 
that runs after classification.

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

Every category has a **Role** — Income, Spend, Investment, Saving, Transfer, or Excluded —
set from the Settings tab, at the parent level (subcategories inherit it, or can override it
individually, e.g. a "Refunds" subcategory under "Income" is Excluded rather than Income).
Roles drive what actually counts as income or spend throughout the dashboard, so a refund or
an internal transfer between your own accounts doesn't inflate your real income or spend.

This is why most of the dashboard shows two versions of the same figures:

- **Standard** — every £ counted by amount sign alone (money in = income, money out = spend),
  regardless of category.
- **Actual** — the same figures computed from each category's Role instead, so refunds,
  transfers, and investment/saving movements are correctly excluded or reclassified.

**Investment vs Saving** are mechanically identical (both net their two directions together —
a contribution/deposit and a later withdrawal are the same account or pot, so £500 in then £200
back out is genuinely £300 net, not two unrelated amounts — and both are excluded from Actual
Income/Spend/Net so a deposit can't be miscounted as real spend) but are kept as two separate
roles rather than one, so a genuine investment contribution (stocks, ISA, pension) is never
silently combined with money moved into a savings pot under one figure. Use whichever fits —
e.g. a Monzo savings pot as Saving, a Trading 212/stocks & shares contribution as Investment.

Transfer/Excluded categories are *not* netted the same way — that bucket can mix genuinely
unrelated inbound and outbound movements (a refund in, an unrelated bill out), so it keeps
showing its outgoing side plus a separate incoming card whenever there's both, rather than
risking two unrelated amounts cancelling into a misleading number.

### New Category Approval

A category is never created silently. When Pass 1/2/3 can't cleanly place a transaction 
under anything that already exists, the transaction is held (it stays unclassified rather 
than being filed under the nearest existing name) and a Telegram card offers up to 3 
candidate placements — each shown as its own button, e.g. "🆕 Tax › Self Assessment" 
alongside "📁 Professional Services › Tax Payments" — with a one-line rationale for each. 
Tap the one that fits and it's created and applied on the next run; tap **❌ None of these** 
and the transaction is released back for reclassification, with every declined name now 
off the table (and no bias toward forcing it into an existing category — a rejection is 
evidence none of the offered ideas were right, existing or new). There is no minimum size; 
a single unusual transaction is reason enough to ask.

### Second-Look Review

New Category Approval only fires when a *new* name is needed, so a confidently wrong 
placement into an existing category (a holding deposit filed under Rent, a dentist 
tapped into Alcohol) would otherwise go straight through. So every placement into an 
existing category — including Telegram quick-taps — gets a second opinion from a 
separate Claude call (`placement_judge.py`) before it is saved. It sees the amounts and 
rhythm of what that subcategory already holds and where the same merchant has been 
filed before, and is told the categories exist to aggregate spending over time, so 
neighbouring-subcategory nitpicks aren't worth raising.

If it objects, nothing changes by itself: the transaction is held behind the same kind 
of Telegram card, offering its suggestion alongside **"Keep it where it was"**. Choosing 
keep is remembered — the same merchant with the same placement isn't questioned again — 
and a transaction is only ever judged once, so declining a card doesn't bring it back. 
"Try again" keeps the keep option. A declined second-look card never counts against 
any category name.

It fails **closed**: if the judge is switched on but can't reach a verdict (API down, 
balance out, unparseable reply), the transaction is left unclassified and retried on the 
next run rather than saved unreviewed — a check that quietly steps aside whenever 
something is wrong only works when nothing is. This applies to quick-taps too. Set
`PLACEMENT_JUDGE=off` in `config/.env` to turn it off deliberately, which releases
anything it was holding back; it is also off when there is no API key.

The card is worded for what it is ("🔍 Second look", the keep option marked ↩️, buttons
"Neither — try again" / "Skip — no change").

#### Reviewing history

Everything classified before the judge existed can get the same second look with
`src/local_scripts/judge_backlog.py`. It costs one API call per transaction, so by default it
only counts them and estimates the cost — nothing is sent, written or held until you add
`--yes`. Objections are held behind the same cards (10 at a time by default, `--cards N` to
change it, `--list-only` to see them without sending any); a held transaction **stays
classified where it is**, so your totals don't move while a card waits, and declining or
ignoring one leaves it exactly as it was. Each transaction is judged once, so re-running only
reviews what's left, and your own decisions (dashboard edits, card choices) are never
second-guessed.

### Failure Alerts

Every Anthropic error the pipeline catches — most importantly **running out of credit** —
also sends you a Telegram alert, with the same bot, so it doesn't 
sit unseen in a log file while transactions quietly stop being classified. Each kind is 
sent at most once per run (a run that fails 40 calls sends one message, not 40): credit 
balance too low, API key rejected, other Claude API errors, and the judge holding 
transactions back. Alerts contain a kind, what to do about it, and counts — never 
transaction text, and never an error's own message, since a parse error quotes the 
model's output. An alert that can't be sent is dropped, and the log line stands.

### Monthly Taxonomy Review

Once a month, the pipeline separately reviews subcategories with at least 8 transactions, 
looking for a coherent cluster inside one that the subcategory name actively misdescribes 
(the kind of pattern that only becomes obvious once several transactions have accumulated — 
Pass 0-3 above only ever see one batch at a time). Proposals are held to a stricter bar 
than real-time proposals — a cluster must be a real, sizeable share of its parent, not 
a rename or a one-off — and go out as their own Telegram cards, capped at 2 per month. 
Nothing changes until you approve one.

## Local Dashboard

A Streamlit dashboard runs on your machine and reads the shared database directly, over the VPN.
Every tab respects a shared sidebar: a quick date-range preset or custom range, parent/subcategory
filters, a text search across merchant/description/context, toggles for skipped/unclassified
transactions, and an **Exclude** block for hiding noise — pick merchants/counterparties from a
dropdown, or paste transaction IDs for one-offs that have no name to match on. Exclusions apply to
the current period, the previous period it's compared against, and the CSV export, so every figure
stays consistent. They last for the browser session only and are never written to the database —
excluding something hides it from view, it does not delete or reclassify it.

| Tab | Description |
|-----|-------------|
| Overview | Standard vs Actual metric cards (Income, Spend, Net, Savings Rate, Invested, Saved, Transferred/Excluded), each with a colored delta badge vs the previous period (direction-aware — e.g. a rise in Spend reads as unfavorable, a rise in Income as favorable). Outgoings/Incomings breakdown charts with a metric selector and Category/Subcategory toggle. |
| Over Time | Average-per-period stat cards; a Savings & Investing chart (Savings Rate %, Investment Rate %, and Actual Savings £ together); standalone Invested and Saved trend charts; Spending Over Time / Income Over Time sections with per-category and per-subcategory monthly trend lines (each with a Total line and a 3-month rolling average overlaid); a Monthly Totals table (collapsed by default) with a tooltip on every column header. |
| Transactions | Full filterable table with a few "fun fact" stat cards up top (unique merchants, biggest transaction, busiest day, etc). Category and Subcategory are edited via dropdowns restricted to your existing taxonomy, so labels stay consistent; Date and Context are also editable; Merchant and Description are read-only. |
| Category Drill-Down | Pick any parent category to see role-based metric cards (Income/Spend/Invested/Saved — Invested/Saved already net deposits against withdrawals; Transferred/Excluded splits into separate Outgoing/Incoming cards whenever a category has both), subcategory breakdown charts, and a Subcategory Summary table with average transaction size, last-seen date, and % of category. |
| Top Merchants | Ranks merchants by Total Spend or Frequency (toggle), with search, a Top-N selector, and stat cards (merchant count, total spend, average per merchant, top merchant). Matches on merchant name or counterparty name, so direct debits and bank transfers that never populate a merchant name are still included. |
| Subscriptions | Auto-detected recurring payments — matches by merchant name *or* counterparty name (many direct debits only populate the latter), requires more supporting occurrences before suggesting a fast-cadence (weekly/fortnightly) pattern, and ignores patterns whose last occurrence is stale relative to their cadence. Confirmed subscriptions auto-deactivate if no matching transaction has landed in the last 2 months. |
| Settings | Stat cards (parent categories, subcategories, % classified). Tree view of all categories with a Role selector per parent (and per-subcategory override) driving the Standard vs Actual figures elsewhere. Add, rename, move, and delete categories — renaming keeps a category's custom Role, and a clear error is shown if a rename would collide with an existing category name. Click any subcategory to view its transactions or bulk-reassign them. Wipe labels per category to force re-classification, or wipe the entire taxonomy structure from the Danger Zone at the bottom (heavily guarded — requires typing a confirmation phrase). Also holds the Backups section (see below). |

### Backups

The database is backed up every night wherever it lives — for this setup, by the data box
(see the data-vault repo) — and every change to a transaction is kept in its history anyway,
so nothing here is needed to stay safe.

**Back up now** in the Settings tab takes a copy of your own before you try something: every
table saved as a Parquet file, in a folder named `<timestamp>_<reason>` under `BACKUP_DIR`
(default `~/bank-enrichment-backups`). Nothing is ever backed up automatically. Read one back
with DuckDB or pandas, e.g.

```sql
SELECT * FROM '~/bank-enrichment-backups/20261025-220341_manual/transactions_raw.parquet';
```

Each copy carries a `manifest.json` recording what it captured, so the Settings tab can tell you
what's in it: transaction count, how many were classified, taxonomy size, subscription count,
the date range covered, and size on disk. The most recent 10 are kept and older ones pruned;
any can also be deleted by hand from the same section (behind a confirmation).

## What You End Up With

A Postgres database (schema `bank_enrichment`) of every transaction:

- The raw bank data exactly as Monzo sent it (amount, merchant, timestamp, counterparty — the full payload)
- Your one-sentence human context (what it actually was), and every answer before it
- Every label it has had — LLM, quick-tap, rule or your own — with when and by what
- A `transactions` view that puts the latest of each together, one row per transaction

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
│   │   └── server_db.py       # Server-side database functions (psycopg)
│   └── local_scripts/         # Runs on your local machine
│       ├── process.py         # The daily run: classify, act on decisions, send summaries
│       ├── worker.py          # Runs process.py every morning, in the server's worker container
│       ├── llm_labelling.py   # LLM classification (Claude) — Passes 0-3
│       ├── category_proposals.py  # Real-time new-category approval (holds transactions behind a Telegram card)
│       ├── taxonomy_review.py # Monthly taxonomy review (cluster detection + Telegram cards)
│       ├── monthly_summary.py # Monthly Telegram summary (spend, income, categories)
│       ├── weekly_summary.py  # Weekly Telegram summary (spend, income, categories)
│       ├── database_functions.py  # The pipeline's and dashboard's database library (psycopg) — includes a CLI (run the file directly) to print stats/transactions
│       ├── pg_copy.py         # One-off: copies the old DuckDB files into Postgres
│       ├── dashboard.py       # Streamlit dashboard
│       └── dashboard_helpers.py   # Pure logic used by the dashboard (role/subscription/chart helpers) — unit tested standalone
├── sql/
│   ├── postgres/schema.sql    # The database: tables, views (applied by the server on every start)
│   ├── tables.sql             # The old local DuckDB schema — read only by pg_copy.py
│   └── server_tables.sql      # The old server DuckDB schema — read only by pg_copy.py
├── config/
│   └── .env.example           # Environment variable template
├── Dockerfile                 # Server container (server dependencies + server files only)
├── docker-compose.yml         # Server + Caddy
└── Caddyfile                  # Reverse proxy + automatic HTTPS
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Server | Python, FastAPI, psycopg |
| Reverse proxy | Caddy (automatic HTTPS) |
| Notifications | Telegram Bot API |
| Database | Postgres 18 on a separate data box; the server and worker reach it on the cloud's private network, the dashboard over WireGuard |
| LLM classification | Anthropic Claude (Sonnet) |
| Local dashboard | Streamlit, Plotly |
| Deployment | Docker Compose: the server, the daily worker, and Caddy |
| CI/CD | GitHub Actions — tests and a server-image build/smoke test run on every PR; merging to `main` auto-deploys over SSH, but only when something the server is built from changed (`src/server_scripts/`, `sql/postgres/schema.sql`, the Dockerfile/compose/Caddy files, `pyproject.toml`, `poetry.lock`) — pipeline and dashboard changes don't restart it. Deploys never overlap. |

## Setup

See [SETUP.md](SETUP.md) for full setup instructions.
