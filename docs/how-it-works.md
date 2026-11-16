# How it works

[← Back to the README](../README.md)

1. Monzo transaction fires a webhook
2. Always-on server stores the raw payload in the Postgres database
3. Server checks your rules — if a rule matches, the transaction is auto-enriched and/or marked as skipped, and Telegram is skipped entirely
4. Otherwise, a Telegram notification is sent to your phone with transaction details
5. You reply with one sentence of context, tap a quick-category button if one's shown, or tap Skip to dismiss
6. Your context is stored as an event alongside the raw transaction — every answer and every label is kept, so nothing is ever overwritten
7. If you don't respond, the system follows up at 1 hour, 1 day, 2 days, and 1 week — then auto-skips
8. Every hour from 08:00 to 22:00 a worker on the server runs the LLM classifier and the rest of the pipeline against the same database — nothing needs your PC to be on
9. LLM classifier (Claude) assigns each transaction a parent category and subcategory using a living taxonomy it builds and refines over time — a genuinely new category is never created silently; it's proposed with a Telegram approval card first (see [New Category Approval](new-categories.md#new-category-approval))
10. Every Monday, a weekly summary is sent to your Telegram — spend, income, net, and a breakdown by category for the week just ended. A monthly summary follows at the end of each month. Once a month, the pipeline also reviews your existing taxonomy for subcategories that have quietly grown a coherent cluster of mislabelled transactions inside them, and proposes a split or move (see [Monthly Taxonomy Review](new-categories.md#monthly-taxonomy-review)).
11. Budgets are checked after each run: a budget message in the morning (on the days you choose), an instant alert when a category is near or over, and `/budget` on demand (see [Budgets](budgets.md))
12. Local Streamlit dashboard lets you explore your spending, view charts, correct labels, track subscriptions and budgets, and ask questions of your data (see [Ask](ask.md))
