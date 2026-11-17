# Ask

[← Back to the README](../../README.md)

Ask anything about your spending, income, savings or subscriptions — on the dashboard's **Ask** tab,
or from your phone by sending the Telegram bot `/ask` and your question (`/ask how much have I spent
this week?`). Follow-ups carry on the conversation (on Telegram, any `/ask` within half an hour); a
bare `/ask` starts afresh.

Claude (Haiku 5.5) answers by calling a few tools over the database: totals counted exactly as the
Overview page counts them, matching transactions, subscriptions, the category list, and, as a last
resort, one read-only SQL query. On the dashboard, **Show workings** under each answer lists every tool
it used, what it asked for and what came back, so any figure can be checked.

- **Read-only, by login.** It reads through its own database login, `bank_enrichment_agent`, which can
  see only the `transaction_roles` view, subscriptions and the category tables (granted in
  `sql/postgres/schema.sql`) and can't write anything. Without that login, Ask is off.
- **A daily limit**, set on the Settings page ($0.30 to begin with), shared by the dashboard and
  Telegram. A typical question costs well under $0.01.
- **Nothing you ask is kept.** Each call's cost, tokens and timing are recorded (the AI usage page), but
  not the question, what the tools read, or the answer, and chats are never saved: one lasts as long
  as the browser tab, or half an hour on Telegram.
- **Charts.** When an answer breaks money down across several categories, merchants or months, the
  agent puts a chart under it. It only chooses *what* to plot; the figures come from the data.
- **Accuracy** is measured by `src/pipeline/agent_eval.py`: questions whose answers are worked out from
  your data, graded automatically. On 13 Nov 2026 Haiku got 15/15, as did Sonnet 5.5 at 15x the cost.

## Settings

On the Settings page's **Ask** tab you can turn the Ask page off, and turn `/ask` on Telegram off. You can
choose the model (Haiku 5.5 by default, or Sonnet 5.5, which costs several times as much per question), and
how hard it thinks (low, medium or high). For Telegram, you can set how many minutes after a question a chat
starts afresh (30 by default), and how long a budget suggestion stays open to a tap (30 by default). The daily
limit is on the same tab and applies to either model.

