# Weekly and monthly summaries

[← Back to the README](../README.md)

The pipeline sends two kinds of Telegram summary, automatically.

**Weekly summary** — sent every Monday for the week just ended (Mon–Sun):

- Total spend and income (skipped transactions excluded)
- Net for the week
- Spend and income broken down by parent category
- Any unclassified amount shown as a separate line so the totals always add up

**Monthly summary** — sent once the month ends, the next time the pipeline runs:

- Total spend and income
- Net (saved or overspent)
- Full breakdown by parent category for both spend and income

The weekly summary also gets this week's budget lines and charts, and the monthly summary gets the month's budgets, a spending chart and a six-month trend (see [Budgets](budgets.md)).

Both summaries catch up automatically if the pipeline was inactive — a summary is sent for each missed period in order, and each is recorded in the database so it is never sent twice.
