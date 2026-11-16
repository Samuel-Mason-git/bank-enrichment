# Bank Enrichment

**Self-hosted personal finance that learns your categories as you go.** Rules and Claude file each payment; when one needs context, Telegram asks with a one-line question and quick taps. Budgets, summaries and a local dashboard sit on top.

[![CI](https://github.com/Samuel-Mason-git/bank-enrichment/actions/workflows/ci.yml/badge.svg)](https://github.com/Samuel-Mason-git/bank-enrichment/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

Most budgeting tools ask you to sort last month's transactions in one go, by which point you've forgotten why you spent £12 at a service station. Bank Enrichment asks about the payments that need it, while you still remember, and keeps what it learns.

## What it does

- **📲 Asks only when it needs context.** A payment that no rule settles and Claude can't place with confidence arrives in Telegram with a one-line question and quick taps for the merchants you pay most. [Details](docs/capture/point-of-purchase.md)
- **📐 Rules.** Match a payment and it's filed, enriched or skipped without asking, and never reaches Telegram. [Rules](docs/capture/rules.md)
- **🧠 Learns your categories.** Claude classifies every payment, proposes new categories for you to approve, and takes a second look at its own placements. [Classification](docs/classification/classification.md), [new categories](docs/classification/new-categories.md), [second look](docs/classification/second-look.md)
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
