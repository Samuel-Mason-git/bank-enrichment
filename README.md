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
3. Telegram notification sent to your phone with transaction details
4. You reply with one sentence of context — or tap Skip to dismiss
5. Enrichment stored alongside the raw transaction in the queue
6. If you don't respond, the system follows up at 1 hour, 1 day, 2 days, and 1 week — then auto-skips

## Dashboard

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

A **Database view** at `/dashboard/db` lets you inspect the raw `stats` and 
`webhook_queue` tables directly without needing to exec into the container.

Credentials are set via environment variables — see SETUP.md.

## What You End Up With

A growing queue of transactions, each with:

- The raw bank data (amount, merchant, timestamp, counterparty)
- Your one-sentence human context (what it actually was)
- Status tracking (pending / enriched / skipped / auto-skipped)

The enriched dataset is designed to feed into a downstream processing step — 
an LLM classification layer, a local database, or any analysis pipeline — 
without ever losing the original context you captured.
