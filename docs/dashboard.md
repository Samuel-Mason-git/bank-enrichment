# Dashboard

[← Back to the README](../README.md)

Everything you see and manage is in the Streamlit dashboard on your own machine (Part 2 of [SETUP.md](../SETUP.md)); the server has no web pages of its own.

## Pages

The pages are grouped in the top bar: **Ask** and **Budget** on their own, then three menus: **Your money** (overview, trends, transactions, drill-downs, merchants, subscriptions), **Manage** (queue, payment details, rules, settings) and **Behind the scenes** (activity, AI usage).

A Streamlit dashboard runs on your machine and reads the shared database directly, over a private connection.
Most tabs respect a shared sidebar (Ask ignores it, and Budget works on its own month): a quick date-range preset or custom range, parent/subcategory
filters, a text search across merchant/description/context, toggles for skipped/unclassified
transactions, and an **Exclude** block for hiding noise — pick merchants/counterparties from a
dropdown, or paste transaction IDs for one-offs that have no name to match on. Exclusions apply to
the current period, the previous period it's compared against, and the CSV export, so every figure
stays consistent. They last for the browser session only and are never written to the database —
excluding something hides it from view, it does not delete or reclassify it.

| Tab | Description |
|-----|-------------|
| Overview | Standard vs Actual metric cards (Income, Spend, Net, Savings Rate, Invested, Saved, Transferred/Excluded), each with a colored delta badge vs the previous period (direction-aware — e.g. a rise in Spend reads as unfavorable, a rise in Income as favorable). Outgoings/Incomings breakdown charts with a metric selector and Category/Subcategory toggle. |
| Ask | Questions about your money in plain English, answered from your own data by an agent (see [Ask](ask.md)). Ignores the sidebar: the dates come from the question. |
| Budget | Monthly budgets per spending category: a fixed 0–100% bar for each, with over-budget spending in red and labelled, a pace line, suggestions for categories without one, a six-month chart, and the Telegram settings (see [Budgets](budgets.md)). |
| Over Time | Average-per-period stat cards; a Savings & Investing chart (Savings Rate %, Investment Rate %, and Actual Savings £ together); standalone Invested and Saved trend charts; Spending Over Time / Income Over Time sections with per-category and per-subcategory monthly trend lines (each with a Total line and a 3-month rolling average overlaid); a Monthly Totals table (collapsed by default) with a tooltip on every column header. |
| Transactions | Full filterable table with a few "fun fact" stat cards up top (unique merchants, biggest transaction, busiest day, etc). Category and Subcategory are edited via dropdowns restricted to your existing taxonomy, so labels stay consistent; Date and Context are also editable; Merchant and Description are read-only. |
| Category Drill-Down | Pick any parent category to see role-based metric cards (Income/Spend/Invested/Saved — Invested/Saved already net deposits against withdrawals; Transferred/Excluded splits into separate Outgoing/Incoming cards whenever a category has both), subcategory breakdown charts, and a Subcategory Summary table with average transaction size, last-seen date, and % of category. |
| Top Merchants | Ranks merchants by Total Spend or Frequency (toggle), with search, a Top-N selector, and stat cards (merchant count, total spend, average per merchant, top merchant). Matches on merchant name or counterparty name, so direct debits and bank transfers that never populate a merchant name are still included. |
| Subscriptions | Auto-detected recurring payments — matches by merchant name *or* counterparty name (many direct debits only populate the latter), requires more supporting occurrences before suggesting a fast-cadence (weekly/fortnightly) pattern, and ignores patterns whose last occurrence is stale relative to their cadence. Confirmed subscriptions auto-deactivate if no matching transaction has landed in the last 2 months. |
| Queue | Payments still waiting for something: an answer from you, a category from the classifier, or a new-category card you haven't picked from. Shows how long each has waited and how often you've been asked. Pick one to answer, skip or delete it. |
| Payment details | One payment in full: every field Monzo sent, what it has now, everything that's happened to it (arriving, questions, answers, labels, reviews), and the raw payload. Answer, skip, unskip or delete it from here. |
| Rules | Every rule, against every payment ever received rather than recent ones. Shows what a rule would catch as you type it, what each saved rule has caught, and which rules catch a given payment. |
| Settings | Stat cards (parent categories, subcategories, % classified). Tree view of all categories with a Role selector per parent (and per-subcategory override) driving the Standard vs Actual figures elsewhere. Add, rename, move, and delete categories — renaming keeps a category's custom Role, and a clear error is shown if a rename would collide with an existing category name. Click any subcategory to view its transactions or bulk-reassign them. Wipe labels per category to force re-classification, or wipe the entire taxonomy structure from the Danger Zone at the bottom (heavily guarded — requires typing a confirmation phrase). Also holds the Ask page's daily spending limit and the Backups section (see [Backups](backups.md)). |
| Activity | What the app did on any day: each hourly run (and any it missed), every warning and error from the server and the worker, and everything that happened to your payments. |
| AI usage | What the calls to Claude cost and did: spend by day, job, model and prompt version; each call's tokens, cost, response and thinking; and how good the answers were (how often you corrected the classifier, and what the placement judge caught). |
