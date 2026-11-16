# Budgets

[← Back to the README](../README.md)

A budget is a monthly amount for one spending category, for example Food & Drink £500. Only spending categories can have one. Income, savings, investments and transfers can't.

**What counts.** A category's spending is its outgoing spend-role money, the same way the Overview counts it. Transfers, pot moves and refunds never use up a budget. Skipped transactions are left out. An unclassified payment counts once it's classified.

**The Budget page** (top bar, beside Ask):
- Set a monthly amount per category. Categories without one get a suggestion: the average of your last three full months, rounded up to £10. Setting a budget to 0 removes it.
- Summary cards: budgeted, spent, left, and where the month is heading at its current rate.
- A bar per category on a fixed 0–100% scale, so they compare directly. A category that's over its budget fills to the line in red and is labelled with how far over and by how much. The solid dark line is where spending would be by now if spread evenly through the month. The dashed line is the budget.
- A six-month chart of spending against your current budgets.

**Telegram messages.** Set on the Budget page, under the summary:
- **The budget message** (text, and a chart of bars): off, every morning, weekdays only, on the days of the week you choose (twice a week is two days), or once a month on a day you pick, e.g. payday. Mondays is the default. The worker sends it at its first run on that day, from 08:00, so a morning message is there when you wake up.
- **Alerts**, each at most once per category per month, each on or off: 80% used, over budget, and off pace (from day 7, heading for more than 10% over).
- The weekly summary gets the budget lines and charts for the newest week. The monthly summary gets the budget lines, a spending-by-category chart, and a six-month trend.
- **`/budget`** sends this month's budgets on demand: the text and the chart. It works only from your own chat.

**The Ask agent** can read budgets ("am I on track?"), and can suggest a budget for you to confirm. Its suggestion shows a **Set it / Dismiss** card on the Ask page, and two buttons on Telegram. Nothing changes until you press Set it. A Telegram suggestion expires after 30 minutes.

**Where it's kept.** Budgets live in the `budgets` table, so they're in any backup of the database (see [Backups](backups.md)). The alerts already sent are kept in `budget_alerts`, so an alert isn't repeated.
