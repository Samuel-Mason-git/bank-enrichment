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

This system sends you a notification the moment a transaction happens and asks 
for three things: a one-line description, a category, and a tag. That's it. 
Thirty seconds while the memory is still there.

## Why Store Context Separately From Labels?

Labels change. The categories you care about today won't be the same in five 
years. By storing your raw context separately from the classification layer, 
you can re-run labelling at any point using a new taxonomy — without losing 
any of your original data.

Feed your enriched transaction history to an LLM in 2030 with a completely 
different set of categories and it will reclassify everything correctly, 
because the context sentences tell it exactly what each transaction was.

## How It Works

1. Monzo transaction fires a webhook
2. Always-on server receives and stores the raw payload
3. Push notification sent to your phone
4. You provide one sentence of context, a category, and a tag
5. Enrichment stored alongside the raw transaction in a queue
6. Local machine picks up completed transactions via a scheduled job
7. LLM reads context and applies or creates labels from your label taxonomy
8. Final enriched dataset stored locally — queryable by category, tag, label, sublabel, date, merchant, or any combination

## Dashboard

The server exposes a password-protected dashboard at `http://your_server_ip:8000/dashboard` showing:

- Total transactions received
- Breakdown by status (pending, enriched, processed)
- The 20 most recent transactions

Credentials are set via environment variables — see SETUP.md.

## What You End Up With

A clean, structured, personal financial dataset where every transaction has:

- The raw bank data (amount, merchant, timestamp)
- Your human context (what it actually was)
- Your manual categorisation (how you thought about it)
- An LLM classification (how it fits into your label taxonomy)

All linked by primary and foreign keys, so you can join any layer together and query across the full history — *how much did I spend on socialising in 2026*, *what was my biggest impulse spending month*, *show me every work-related expense this year*.