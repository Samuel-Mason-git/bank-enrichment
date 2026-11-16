# Bank Enrichment

**Self-hosted personal finance that works out what your transactions meant, not just their category.**

[![CI](https://github.com/Samuel-Mason-git/bank-enrichment/actions/workflows/ci.yml/badge.svg)](https://github.com/Samuel-Mason-git/bank-enrichment/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

## What is this?

Most financial tools answer one question: *what category was this transaction?* Bank Enrichment answers a different one: *what did this transaction actually mean?*

A transaction like `TESCO £43.20` is almost meaningless on its own. But `weekly shop, ingredients for a dinner party, plus impulse snacks` is something you can reason about, now and in ten years' time. So the app captures that context at the moment of purchase, while you still remember it, and keeps it apart from the categories.

Categories will change, so the context is the record and the labels are derived from it.

It learns as it goes. It remembers which categories you use for each merchant, so the quick taps get closer to what you'd pick. The taxonomy evolves too: a new category is only created when you approve it on Telegram, and a monthly review proposes splits for subcategories that have grown muddled. Each placement into an existing category gets a second look from a separate Claude call, the placement judge, before it's saved. If that check can't reach a verdict, the payment waits rather than being saved unreviewed. Rules file the payments you've already settled. For the rest, Telegram asks one question, quick taps offer the categories you use for each merchant, and your answer is the context Claude classifies from. Because the context is stored separately from the labels, you can reclassify everything under a new set of categories without losing anything.

## What it does

- **📲 Asks about what rules don't cover.** A payment no rule settles arrives in Telegram with a one-line question and quick taps for the merchants you pay most. Your answer, or a tap, is the context Claude classifies from. [Details](docs/capture/point-of-purchase.md)
- **📐 Rules.** Match a payment and it's filed, enriched or skipped without asking, and never reaches Telegram. [Rules](docs/capture/rules.md)
- **🧠 Learns your categories.** It remembers the categories you use for each merchant, and Claude classifies every payment. New categories need your approval, a second look (the placement judge) checks each placement, and a monthly review proposes splits. [Classification](docs/classification/classification.md), [new categories](docs/classification/new-categories.md), [second look](docs/classification/second-look.md)
- **🔁 Subscriptions.** Recurring payments are spotted automatically. [Subscriptions](docs/capture/subscriptions.md)
- **🎯 Budgets per category.** Monthly limits with Telegram messages, alerts, `/budget` on demand and charts. [Budgets](docs/money/budgets.md)
- **💬 Ask your money.** Plain-English questions answered by a read-only agent that shows its workings, on the dashboard or with `/ask` on Telegram. [Ask](docs/money/ask.md)
- **📨 Weekly and monthly summaries** sent to Telegram, catching up on any missed period. [Summaries](docs/money/summaries.md)
- **📊 A local dashboard.** Overview, trends, transactions, merchants, subscriptions, budgets, rules, and what every Claude call cost. [Dashboard](docs/money/dashboard.md), [AI usage](docs/classification/ai-usage.md)
- **🗄️ Your data, your database.** Postgres wherever you choose, every raw webhook kept exactly as Monzo sent it, and a copy you take yourself from Settings. [Your data](docs/data/your-data.md), [Backups](docs/data/backups.md)

## Who it's for

One person's Monzo account, self-hosted. It's light: the server and worker together run comfortably on a small 1 GB server with swap, which is what it was built on. It isn't a hosted product and there's no sign-up. It's shared as a working example of an LLM-backed pipeline that's been run for real every day, with the warts left in.

## What you need

A Monzo account with developer access, a Telegram bot, an Anthropic API key, a server with Docker, and a domain for HTTPS (DuckDNS is free). The [SETUP guide](SETUP.md) walks through each step.

## How it works

A Monzo payment fires a webhook. The server stores it, then either applies a rule or asks you on Telegram. Every hour a worker on the server classifies what's pending with Claude and runs the checks. Summaries, budget messages and alerts go out on their schedules. [The full walkthrough](docs/capture/how-it-works.md) covers each step.

## Documentation

| Doc | What it covers |
|-----|----------------|
| [How it works](docs/capture/how-it-works.md) | The whole pipeline, from a Monzo payment to a Telegram card |
| [Point of purchase](docs/capture/point-of-purchase.md) | Telegram questions, quick categories and skipping |
| [Rules](docs/capture/rules.md) | Fixed categories, auto-context, auto-skip and rule suggestions |
| [Subscriptions](docs/capture/subscriptions.md) | Recurring payments, spotted automatically |
| [Classification](docs/classification/classification.md) | How Claude places each payment, and the default taxonomy |
| [Categories and roles](docs/classification/categories-and-roles.md) | Income, spend, investment and saving, and Standard vs Actual |
| [New categories](docs/classification/new-categories.md) | Approving new categories, and the monthly taxonomy review |
| [Second look](docs/classification/second-look.md) | The check on each placement before it's saved |
| [Summaries](docs/money/summaries.md) | Weekly and monthly Telegram summaries |
| [Budgets](docs/money/budgets.md) | Monthly limits, Telegram messages, alerts and charts |
| [Ask](docs/money/ask.md) | Questions about your money, on the dashboard or Telegram |
| [Dashboard](docs/money/dashboard.md) | Every page, and the shared filters |
| [AI usage](docs/classification/ai-usage.md) | What each Claude call cost, and which models are used |
| [Failure alerts](docs/classification/failure-alerts.md) | Telegram alerts when Claude calls fail |
| [Backups](docs/data/backups.md) | Taking a copy of your data from Settings |
| [Your data](docs/data/your-data.md) | What's stored, and how it's kept |
| [Security](docs/data/security.md) | Who can reach what |
| [Architecture](docs/data/architecture.md) | Project structure, tech stack and CI/CD |

## Setup

See [SETUP.md](SETUP.md) for full setup instructions.
