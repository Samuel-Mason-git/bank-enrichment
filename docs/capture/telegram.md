# Telegram

[← Back to the README](../../README.md)

The bot talks only to your own chat (`TELEGRAM_CHAT_ID`). Anyone else who messages it is ignored.

## Commands

| Command | What it does |
|---|---|
| `/ask <question>` | Asks the Ask agent about your money, e.g. `/ask how much did I spend this week?`. A follow-up within the chat timeout carries on the conversation. See [Ask](../money/ask.md). |
| `/ask` on its own | Starts a fresh chat. |
| `/budget` | This month's budgets: the text, and a chart of the bars in one message when the text fits a photo caption (1,024 characters). Otherwise the chart comes first, then the text. See [Budgets](../money/budgets.md). |
| `/help` | The commands, and what a plain message does. |

`/ask` and `/budget` can be switched off, or their timings changed, on the Settings page's Ask and Budgets tabs.

## Plain messages

A plain message (anything that isn't a command) answers the payment the bot has just asked about: a sentence of context, or Skip. A category button is the same as typing that category as the context. See [Asking at the point of purchase](point-of-purchase.md).

## Messages the bot sends you

- **A payment question** for each payment no rule covers, with quick taps for that merchant's usual categories.
- **Budget messages and alerts**, on the days you choose in Settings → Budgets.
- **Weekly and monthly summaries**. See [Summaries](../money/summaries.md).
- **Unusual payments and subscription changes**, from the Alerts tab. See [Point of purchase](point-of-purchase.md#unusual-payments) and [Subscriptions](subscriptions.md#alerts).
- **Failure alerts** when Claude calls fail. See [Failure alerts](../classification/failure-alerts.md).
- **Category and rule decisions** that need your approval. See [New categories](../classification/new-categories.md) and [Rules](rules.md).

## Setting it up

The BotFather step in [SETUP.md](../../SETUP.md) makes `/ask`, `/budget` and `/help` appear as you type.
